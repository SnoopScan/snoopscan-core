"""Keep what a fleet benchmark measured, so drift is visible over time.

A single run answers "does the engine read the web today". Kept, successive
runs answer the more useful question: what changed, and when. The weekly smoke
test asks the same thing of five URLs; this asks it of hundreds, and records
per-site detail rather than a pass rate.

The column that matters most is `self_inflicted`: the run fetches every failed
URL again with a plain HTTP client, and if that plain client got the page while
the engine did not, the failure is ours. Without it a benchmark says "23 sites
failed" and nobody can tell a hostile site from a bug of our own — which is how
a page carrying a Turnstile widget spent a week reading as blocked.
"""

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE benchmark_runs (
            id          text PRIMARY KEY,
            label       text NOT NULL,
            started_at  timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz,
            engine_sha  text,
            total       integer NOT NULL DEFAULT 0,
            passed      integer NOT NULL DEFAULT 0,
            notes       text
        )
        """
    )
    op.execute(
        """
        CREATE TABLE benchmark_results (
            id              bigserial PRIMARY KEY,
            run_id          text NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
            rank            integer,
            domain          text NOT NULL,
            url             text NOT NULL,
            ok              boolean NOT NULL,
            error_code      text,
            signal          text,
            tier            text,
            tiers_attempted text,
            words           integer,
            credits         integer,
            proxy_bytes     bigint,
            elapsed_ms      integer,
            -- The control fetch, run only when the engine failed.
            control_ok      boolean,
            control_words   integer,
            self_inflicted  boolean NOT NULL DEFAULT false,
            recorded_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_benchmark_results_run ON benchmark_results (run_id)")
    op.execute("CREATE INDEX idx_benchmark_results_domain ON benchmark_results (domain)")
    op.execute(
        "CREATE INDEX idx_benchmark_results_blame ON benchmark_results (self_inflicted) "
        "WHERE self_inflicted"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS benchmark_results")
    op.execute("DROP TABLE IF EXISTS benchmark_runs")
