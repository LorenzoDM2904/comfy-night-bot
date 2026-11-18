"""Job store: the only owner of the `jobs` and `inputs` tables.

Job creation, draft edits, attached images, the lifecycle, delivery and
reads live here. The rest of the bot writes no SQL on these tables: it
uses JobStore's named operations.

Operations return True/False when they may not happen (e.g. the draft
was confirmed meanwhile): messages for the user are left to the callers.

Chains (for plugins, see plugins.JobKind): a container draft that, when
started, creates child jobs (parent_job_id) that wait for each other
(depends_on), plus any jobs the bot runs itself instead of ComfyUI
(run). Plugins declare the kinds: here they are read with
plugins.kinds(...).
"""

import json
import time
import uuid

from . import plugins

# Draft fields that can change before confirmation (for a container
# draft only those in its JobKind.edit_fields).
DRAFT_FIELDS = ("prompt", "component", "character", "light", "overrides")

# The job lifecycle: step -> (source states, target state). It is the
# single source of truth: every state change goes through here, and it
# happens only if the job is still in one of the source states.
# Derived jobs are born directly 'queued' (enqueue_derived).
LIFECYCLE = {
    "confirm":        (("draft",), "queued"),
    "cancel":         (("draft", "queued", "preparing"), "cancelled"),
    "claim":          (("queued",), "preparing"),
    "release":        (("preparing",), "queued"),
    "begin_submit":   (("preparing",), "submitting"),
    "mark_running":   (("submitting",), "running"),
    "complete":       (("running",), "done"),
    "fail":           (("submitting", "running"), "failed"),
    "mark_uncertain": (("submitting", "running"), "unknown"),
    "skip":           (("unknown",), "skipped"),
    "retry":          (("failed", "unknown", "skipped"), "queued"),
    # A job the bot runs itself (JobKind.run, never in ComfyUI): after
    # claim it closes without going through submitting/running.
    "run_done":       (("preparing",), "done"),
    "run_fail":       (("preparing",), "failed"),
    # Part of a chain redone (redo_chain): the unfinished jobs it
    # replaces are closed (finished ones stay).
    "supersede":      (("queued", "failed", "unknown", "skipped"), "cancelled"),
    # A started container draft is finished when the plugin says so
    # (for example at the first edit).
    "finish_container": (("queued",), "done"),
}

# States in which the job occupies (or is about to occupy) ComfyUI.
ACTIVE = ("queued", "preparing", "submitting", "running")

# Every job it depends on is done. A job waiting for a failed one stays
# queued until that job succeeds (/retry) or is cancelled (and then it is
# cancelled too).
READY = (
    "NOT EXISTS(SELECT 1 FROM json_each(COALESCE(jobs.depends_on,'[]')) "
    "AS d JOIN jobs AS dep ON dep.id=d.value WHERE dep.state<>'done')"
)


def _kind_in(kinds, negate=False):
    """«kind IN(...)» with literal values (kinds are names checked by the
    plugin registry: letters, digits and _)."""
    if not kinds:
        return "1" if negate else "0"
    values = ",".join(f"'{kind}'" for kind in kinds)
    return f"kind {'NOT ' if negate else ''}IN({values})"


def runnable():
    """A queued job that can start in ComfyUI: neither a started
    container draft (it never goes to ComfyUI) nor a job the bot runs
    itself (next_run), and the jobs it depends on are done."""
    skip = plugins.kinds(container=True) + plugins.kinds(run=True)
    return f"state='queued' AND {_kind_in(skip, negate=True)} AND {READY}"

# Delivery of a finished job, kept apart from its state. An interrupted
# delivery becomes 'unknown' and is repeated only with /result: never two
# deliveries at once, never a regeneration.
DELIVERY = {
    "begin":   (("pending", "sent", "unknown"), "sending"),
    "sent":    (("sending",), "sent"),
    "lost":    (("sending",), "unknown"),
}


class JobStore:
    def __init__(self, conn):
        self.conn = conn

    # --- reads ----------------------------------------------------------------

    def get(self, job_id):
        """The job, or None if it does not exist."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE id=?", (job_id,)
        ).fetchone()

    def owned(self, job_id, owner):
        """The job of `owner`; ValueError if it does not exist or is not theirs."""
        job = self.get(job_id)
        if not job or job["owner"] != owner:
            raise ValueError("No such job, or not accessible.")
        return job

    def open_jobs(self, owner, limit=80):
        """The user's jobs not closed yet (/queue). The chain jobs of a
        cancelled container draft are left out even if they failed: the
        draft will not go on and /retry does not pick them up. The limit
        is generous because a single chain makes about ten jobs."""
        chain = _kind_in(plugins.kinds(chain=True))
        return self.conn.execute(
            "SELECT * FROM jobs WHERE owner=? "
            "AND state NOT IN('done','cancelled','skipped') "
            f"AND NOT ({chain} AND EXISTS("
            "SELECT 1 FROM jobs AS p WHERE p.id=jobs.parent_job_id "
            "AND p.state='cancelled')) "
            "ORDER BY id LIMIT ?",
            (owner, limit),
        ).fetchall()

    def active_job(self):
        """The job already sent to ComfyUI (or being sent), if any: it is
        followed to its outcome before another one starts."""
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state IN('submitting','running') "
            "ORDER BY id LIMIT 1"
        ).fetchone()

    def next_runnable(self, window_open):
        """The next job to start. run_now jobs (derived ones, «now»
        variants) start even with the window closed and come before the
        others."""
        if window_open:
            return self.conn.execute(
                f"SELECT * FROM jobs WHERE {runnable()} "
                "ORDER BY run_now DESC, id LIMIT 1"
            ).fetchone()
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE {runnable()} AND run_now=1 "
            "ORDER BY id LIMIT 1"
        ).fetchone()

    def next_uncertain(self):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state='unknown' ORDER BY id LIMIT 1"
        ).fetchone()

    def next_pending_delivery(self):
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state='done' AND delivery='pending' "
            "ORDER BY id LIMIT 1"
        ).fetchone()

    def needs_comfy(self, window_open):
        """Is ComfyUI needed now? Yes if a job is running, has a delivery
        to make, has an uncertain outcome to close or can start."""
        busy = self.conn.execute(
            "SELECT 1 FROM jobs WHERE "
            "state IN('preparing','submitting','running','unknown') "
            "OR (state='done' AND delivery IN('pending','sending')) LIMIT 1"
        ).fetchone()
        return busy is not None or self.next_runnable(window_open) is not None

    def scene_inputs(self, job_id):
        """Paths of the draft's images, in message order."""
        return [
            row["path"] for row in self.conn.execute(
                "SELECT path FROM inputs WHERE job_id=? ORDER BY message_id",
                (job_id,),
            )
        ]

    # --- creation -------------------------------------------------------------

    def create_draft(self, owner, chat, prompt, component="", origin="draft"):
        """Always creates a new draft. `origin` says where it comes from
        (readable in the origin key, only useful for debugging)."""
        return self._insert_draft(
            f"{origin}:{uuid.uuid4().hex}", owner, chat, prompt, component,
        )

    def album_draft(self, owner, chat, album, prompt, component=""):
        """The draft that gathers an album's photos: the first photo
        creates it, the next ones find it. A photo that arrives after the
        album was confirmed opens a new draft."""
        key = f"{chat}:album:{album}"
        job = self.conn.execute(
            "SELECT * FROM jobs WHERE source_key=?", (key,)
        ).fetchone()
        if job is None:
            return self._insert_draft(key, owner, chat, prompt, component)
        if job["owner"] != owner:
            raise ValueError("The album belongs to another user.")
        if job["state"] != "draft":
            return self.create_draft(
                owner, chat, prompt, component, origin=f"{key}:late",
            )
        return job

    def create_variant(self, source, chat, light, overrides, run_now):
        """A /matrix variant: a draft that copies prompt, component,
        character and images of `source`, with its own light and
        overrides. Returns the id."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO jobs(source_key,owner,chat,component,character,"
                "prompt,token,created,parent_job_id,run_now,light,overrides) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{source['owner']}:matrix:{source['id']}:"
                    f"{uuid.uuid4().hex}",
                    source["owner"], chat, source["component"],
                    source["character"], source["prompt"], uuid.uuid4().hex,
                    time.time(), source["id"], 1 if run_now else 0,
                    light, json.dumps(overrides),
                ),
            )
            self.conn.execute(
                "INSERT INTO inputs(job_id,message_id,path) "
                "SELECT ?, message_id, path FROM inputs WHERE job_id=?",
                (cur.lastrowid, source["id"]),
            )
        return cur.lastrowid

    def enqueue_derived(self, parent, kind, snapshot, character=None):
        """Queues a derived job of `parent`: it is born queued, with
        run_now and the snapshot already serialised. Returns (id,
        created): while one of the same kind is active, a double tap
        returns that one instead of creating another."""
        active = self.conn.execute(
            "SELECT id FROM jobs WHERE parent_job_id=? AND kind=? "
            f"AND state IN({_marks(ACTIVE)}) ORDER BY id LIMIT 1",
            (parent["id"], kind, *ACTIVE),
        ).fetchone()
        if active:
            return active["id"], False
        cur = self.conn.execute(
            "INSERT INTO jobs(source_key, owner, chat, component, character, "
            "prompt, state, token, snapshot, created, kind, parent_job_id, "
            "run_now) VALUES(?,?,?,?,?,'','queued',?,?,?,?,?,1)",
            (
                f"{kind}:{parent['id']}:{uuid.uuid4().hex}",
                parent["owner"], parent["chat"], kind, character,
                uuid.uuid4().hex, snapshot, time.time(),
                kind, parent["id"],
            ),
        )
        self.conn.commit()
        return cur.lastrowid, True

    def create_container(self, owner, chat, kind, character, prompt,
                         inputs, origin, extra=None):
        """A plugin's container draft (JobKind with container): the
        inputs [(message_id, path)] and, in the same transaction,
        extra(conn, job_id) for the plugin's tables."""
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO jobs(source_key,owner,chat,component,character,"
                "prompt,token,created,kind) VALUES(?,?,?,?,?,?,?,?,?)",
                (f"{origin}:{uuid.uuid4().hex}", owner, chat, "", character,
                 prompt, uuid.uuid4().hex, time.time(), kind),
            )
            job_id = cur.lastrowid
            self.conn.executemany(
                "INSERT INTO inputs(job_id,message_id,path) VALUES(?,?,?)",
                [(job_id, message_id, str(path)) for message_id, path in inputs],
            )
            if extra:
                extra(self.conn, job_id)
        return self.get(job_id)

    def children(self, parent_id, kinds):
        """The child jobs of `parent_id` of kinds `kinds`, in order."""
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE parent_job_id=? AND {_kind_in(kinds)} "
            "ORDER BY id",
            (parent_id,),
        ).fetchall()

    def start_chain(self, job_id, snapshot, children, final=None):
        """Starts a container draft: the parent becomes queued (it never
        runs: see runnable) and the chain jobs and the final one are
        created, all in a single transaction. False if it is not (or no
        longer) a draft.

        `children`: dicts with ref (a unique name), kind, segment,
        component, prompt, snapshot (JSON), after (the refs it depends
        on). In the snapshot's assets "@ref" becomes "job:<id>": that
        job's output, resolved by the worker at submission time.
        `final`: a dict with kind, snapshot and after (for example the
        final edit)."""
        parent = self.get(job_id)
        containers = _kind_in(plugins.kinds(container=True))
        with self.conn:
            cur = self.conn.execute(
                "UPDATE jobs SET state='queued', snapshot=? "
                f"WHERE id=? AND state='draft' AND {containers}",
                (snapshot, job_id),
            )
            if cur.rowcount != 1:
                return False
            ids = self._insert_chain(parent, children)
            if final:
                self._insert_final(parent, final, ids)
        return True

    def redo_chain(self, parent_id, superseded, children, final):
        """Redoes part of a started chain: the unfinished `superseded`
        jobs are closed, the new jobs are created (`children`, as in
        start_chain; `after` may also hold ids of existing jobs) plus a
        new final job, all in a single transaction. Finished jobs stay:
        their outputs are the chain's history."""
        parent = self.get(parent_id)
        sources, target = LIFECYCLE["supersede"]
        with self.conn:
            for job_id in superseded:
                # Like supersede(), inside the transaction.
                self.conn.execute(
                    f"UPDATE jobs SET state=?, error=? WHERE id=? "
                    f"AND state IN({_marks(sources)})",
                    (target, "replaced by a new attempt", job_id,
                     *sources),
                )
            ids = self._insert_chain(parent, children)
            self._insert_final(parent, final, ids)
        return ids

    def _insert_chain(self, parent, children):
        ids = {}
        for child in children:
            snap = json.loads(child["snapshot"])
            snap["assets"] = [
                f"job:{ids[a[1:]]}" if a.startswith("@") else a
                for a in snap["assets"]
            ]
            ids[child["ref"]] = self._insert_child(
                parent, child["kind"], child["segment"], child["component"],
                child["prompt"], json.dumps(snap),
                _resolve(child.get("after", ()), ids),
            )
        return ids

    def _insert_final(self, parent, final, ids):
        return self._insert_child(
            parent, final["kind"], None, "", "", final["snapshot"],
            _resolve(final["after"], ids),
        )

    def _insert_child(self, parent, kind, segment, component, prompt,
                      snapshot, depends):
        cur = self.conn.execute(
            "INSERT INTO jobs(source_key,owner,chat,component,"
            "character,prompt,state,token,snapshot,created,kind,"
            "parent_job_id,segment,depends_on) "
            "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)",
            (
                f"{kind}:{parent['id']}:{segment}:{uuid.uuid4().hex}",
                parent["owner"], parent["chat"], component,
                parent["character"], prompt, uuid.uuid4().hex, snapshot,
                time.time(), kind, parent["id"], segment, json.dumps(depends),
            ),
        )
        return cur.lastrowid

    def next_run(self):
        """The next job for the bot to run (JobKind.run) that is ready:
        every job it depends on is done."""
        return self.conn.execute(
            f"SELECT * FROM jobs WHERE state='queued' AND "
            f"{_kind_in(plugins.kinds(run=True))} AND {READY} "
            "ORDER BY id LIMIT 1"
        ).fetchone()

    def _insert_draft(self, key, owner, chat, prompt, component):
        cur = self.conn.execute(
            "INSERT INTO jobs(source_key,owner,chat,component,prompt,token,"
            "created) VALUES(?,?,?,?,?,?,?)",
            (key, owner, chat, component or "", prompt,
             uuid.uuid4().hex, time.time()),
        )
        self.conn.commit()
        return self.get(cur.lastrowid)

    # --- lifecycle ------------------------------------------------------------

    def confirm(self, job_id, snapshot, component):
        """Draft -> queued, with the immutable snapshot to run and the
        component used (even when confirmation chose it). `snapshot` is
        already serialised (Snapshot.to_json)."""
        return self._move(
            job_id, "confirm", snapshot=snapshot, component=component,
        )

    def cancel(self, job_id, error=None):
        """Cancels the job and, in cascade, the queued jobs that depend on
        it (or belong to a container draft's chain)."""
        if not self._move(job_id, "cancel", error=error):
            return False
        gathers = plugins.kinds(gathers=True)
        for waiting in self._waiting_on(job_id):
            if waiting["kind"] in gathers \
                    and waiting["parent_job_id"] != job_id:
                self._drop_dependency(waiting, job_id)
            else:
                self.cancel(
                    waiting["id"], error=f"cancelled with job #{job_id}",
                )
        return True

    def _waiting_on(self, job_id):
        chain = _kind_in(plugins.kinds(chain=True))
        return self.conn.execute(
            "SELECT * FROM jobs WHERE state='queued' AND ("
            f"(parent_job_id=? AND {chain}) OR EXISTS("
            "SELECT 1 FROM json_each(COALESCE(jobs.depends_on,'[]')) "
            "WHERE value=?)) ORDER BY id",
            (job_id, job_id),
        ).fetchall()

    def _drop_dependency(self, gatherer, job_id):
        """A cancelled job leaves the list of a JobKind.gathers job (for
        example a clip leaves the edit, which joins the others). If
        nothing is left, that job and its parent are cancelled."""
        # Read again: a cascading cancel may have just changed it.
        gatherer = self.get(gatherer["id"])
        depends = [d for d in json.loads(gatherer["depends_on"]) if d != job_id]
        if depends:
            self.conn.execute(
                "UPDATE jobs SET depends_on=? WHERE id=?",
                (json.dumps(depends), gatherer["id"]),
            )
            self.conn.commit()
            return
        error = "no jobs left to wait for"
        self.cancel(gatherer["id"], error=error)
        self.cancel(gatherer["parent_job_id"], error=error)

    def claim(self, job_id):
        """The worker claims the job, before the uploads: a /cancel that
        arrived meanwhile wins."""
        return self._move(job_id, "claim")

    def release(self, job_id):
        """Preparation failed: the job goes back to the queue."""
        return self._move(job_id, "release")

    def run_done(self, job_id, outputs):
        """A job run by the bot succeeded (after claim): done, to be
        delivered."""
        return self._move(
            job_id, "run_done",
            outputs=json.dumps(outputs), delivery="pending", error=None,
            started=time.time(),
        )

    def run_fail(self, job_id, error):
        return self._move(job_id, "run_fail", error=error)

    def supersede(self, job_id):
        """A chain job replaced by a new attempt (see redo_chain)."""
        return self._move(
            job_id, "supersede", error="replaced by a new attempt",
        )

    def finish_container(self, job_id):
        """The started container draft is finished."""
        return self._move(job_id, "finish_container")

    def begin_submit(self, job_id):
        """The intent to submit, recorded BEFORE contacting ComfyUI."""
        return self._move(job_id, "begin_submit", started=time.time())

    def mark_running(self, job_id, prompt_id):
        return self._move(job_id, "mark_running", prompt_id=prompt_id)

    def complete(self, job_id, outputs):
        """Outputs downloaded: the job is done and delivery is pending."""
        return self._move(
            job_id, "complete",
            outputs=json.dumps(outputs), delivery="pending", error=None,
        )

    def fail(self, job_id, error):
        return self._move(job_id, "fail", error=error)

    def mark_uncertain(self, job_id, reason):
        return self._move(job_id, "mark_uncertain", error=reason)

    def skip(self, job_id, error):
        return self._move(job_id, "skip", error=error)

    def retry(self, job_id):
        """A new attempt allowed: a new token, no trace of the previous
        submission."""
        return self._move(
            job_id, "retry",
            token=uuid.uuid4().hex, prompt_id=None, started=None, error=None,
        )

    def release_interrupted(self):
        """At startup: 'preparing' means nothing was sent (the intent to
        submit is 'submitting'), so the job starts over."""
        sources, target = LIFECYCLE["release"]
        self.conn.execute(
            f"UPDATE jobs SET state=? WHERE state IN({_marks(sources)})",
            (target, *sources),
        )
        self.conn.commit()

    # --- delivery -------------------------------------------------------------

    def begin_delivery(self, job_id):
        """Marks the delivery before touching the network: a crash half
        way leaves 'sending', which becomes 'unknown' at startup. False if
        the job is not done or a delivery is already in progress."""
        sources, target = DELIVERY["begin"]
        cur = self.conn.execute(
            f"UPDATE jobs SET delivery=? WHERE id=? AND state='done' "
            f"AND delivery IN({_marks(sources)})",
            (target, job_id, *sources),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def finish_delivery(self, job_id, sent):
        """Closes a delivery in progress: 'sent' or 'unknown'."""
        sources, target = DELIVERY["sent" if sent else "lost"]
        cur = self.conn.execute(
            f"UPDATE jobs SET delivery=? WHERE id=? "
            f"AND delivery IN({_marks(sources)})",
            (target, job_id, *sources),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def recover_interrupted_deliveries(self):
        """At startup: a delivery left 'sending' was interrupted by a
        crash. It becomes 'unknown': it is repeated with /result."""
        sources, target = DELIVERY["lost"]
        self.conn.execute(
            f"UPDATE jobs SET delivery=? WHERE delivery IN({_marks(sources)})",
            (target, *sources),
        )
        self.conn.commit()

    def _move(self, job_id, step, **fields):
        sources, target = LIFECYCLE[step]
        columns = "".join(f", {key}=?" for key in fields)
        cur = self.conn.execute(
            f"UPDATE jobs SET state=?{columns} "
            f"WHERE id=? AND state IN({_marks(sources)})",
            (target, *fields.values(), job_id, *sources),
        )
        self.conn.commit()
        return cur.rowcount == 1

    # --- drafts ---------------------------------------------------------------

    def edit_draft(self, job_id, **fields):
        """Edits a draft. False if it is not (or no longer) a draft: the
        check and the edit are a single statement, so a confirmation that
        arrived meanwhile always wins."""
        unknown = set(fields) - set(DRAFT_FIELDS)
        if unknown:
            raise TypeError(f"Fields that cannot be changed: {sorted(unknown)}")
        container = self._container(job_id)
        if container and set(fields) - set(container.edit_fields):
            raise ValueError(
                container.edit_hint
                or f"Draft #{job_id} cannot be changed this way."
            )
        if not fields:
            return self._is_draft(job_id)
        if "overrides" in fields and fields["overrides"] is not None:
            fields["overrides"] = json.dumps(fields["overrides"])
        columns = ", ".join(f"{key}=?" for key in fields)
        cur = self.conn.execute(
            f"UPDATE jobs SET {columns} WHERE id=? AND state='draft'",
            (*fields.values(), job_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def has_input(self, job_id, message_id):
        return self.conn.execute(
            "SELECT 1 FROM inputs WHERE job_id=? AND message_id=?",
            (job_id, message_id),
        ).fetchone() is not None

    def attach_input(self, job_id, message_id, path):
        """Attaches an image to a draft. Idempotent per (draft, message).
        False if the job is not a draft."""
        if self._container(job_id):
            raise ValueError(f"Photos cannot be added to draft #{job_id}.")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO inputs(job_id,message_id,path) "
            "SELECT ?,?,? WHERE EXISTS("
            "SELECT 1 FROM jobs WHERE id=? AND state='draft')",
            (job_id, message_id, str(path), job_id),
        )
        self.conn.commit()
        return cur.rowcount == 1 or self._is_draft(job_id)

    def _container(self, job_id):
        """The JobKind if the job is a container draft, otherwise None."""
        row = self.conn.execute(
            "SELECT kind FROM jobs WHERE id=?", (job_id,)
        ).fetchone()
        kind = plugins.kind(row["kind"]) if row else None
        return kind if kind and kind.container else None

    def _is_draft(self, job_id):
        return self.conn.execute(
            "SELECT 1 FROM jobs WHERE id=? AND state='draft'", (job_id,)
        ).fetchone() is not None


def _marks(values):
    return ",".join("?" for _ in values)


def _resolve(after, ids):
    """Dependencies: refs of the jobs just created (str) or existing ids."""
    return [ids[ref] if isinstance(ref, str) else ref for ref in after]
