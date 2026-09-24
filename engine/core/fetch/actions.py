"""Run a caller's action sequence inside the live browser page.

`actions` has been part of the request model since the API was frozen, and
until now the only thing the engine did with it was pick a sticky proxy: the
steps were validated, priced at the browser tier, and then dropped. A customer
building a click sequence in the Playground got a 200, a plausible-looking
page, a bill for five credits, and none of their steps performed — with
nothing in the answer to say so. A silent no-op is worse than an error,
because the error at least tells you.

WHERE THIS RUNS. Inside the browser fetch, after the page is ready and BEFORE
the HTML is captured, because the point of clicking is to read what the click
produced. It cannot be bolted on after the fetch returns: the browser context
is closed by then, and a second visit is a fresh session on a fresh exit IP,
which is a different page in every way that matters.

WHAT A FAILING STEP MEANS. A selector that does not match is the caller's
mistake and they have to hear about it, so the sequence stops at the first
failure and the error names the step by index and type. The page is still
captured and returned: a click that missed on step 4 of 6 leaves a real page
behind, and throwing it away would bill them for nothing at all.

STEPS ARE NOT FREE. Each one gets a slice of what remains of the request's own
timeout rather than an open-ended wait, so a selector that never appears
cannot hold a browser slot until the whole request dies.

An explicit captchaCheckbox step permits one bounded widget click. It does
not solve image/audio puzzles or treat a checked widget as page success.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import structlog

logger = structlog.get_logger(__name__)

# The longest any single step may take, whatever the request's own budget is.
# A step is a click or a keystroke; one that wants half a minute is a selector
# that is never going to match.
MAX_STEP_MS = 15_000
# What is left for the capture that follows the sequence. Spending the entire
# budget on actions and then having nothing left to read the page with turns a
# successful sequence into a timeout.
CAPTURE_RESERVE_MS = 3_000
# A scroll "amount" is a wheel notch, in the region of one screen each.
SCROLL_PIXELS = 700


class ActionError(RuntimeError):
    """A step the caller asked for could not be carried out.

    `fault` separates two things that are not the same diagnosis even though
    both name the same step:

      "invalid"  the step cannot be carried out as written — malformed
                 selector syntax, a bad argument. Nothing about the page will
                 change that, and it is the caller's to fix.
      "timeout"  the step was well-formed and the page did not answer it. The
                 selector may be right and the page changed, or it never
                 finished loading, or it showed something other than the page
                 expected. That is not a syntax error and telling someone to
                 fix their selector sends them after the wrong thing.
    """

    def __init__(
        self,
        index: int,
        kind: str,
        detail: str,
        fault: str = "invalid",
        evidence: dict[str, Any] | None = None,
    ) -> None:
        self.evidence = evidence
        self.index = index
        self.kind = kind
        self.detail = detail
        self.fault = fault
        super().__init__(f"action {index} ({kind}) failed: {detail}")


# What Playwright says when the selector itself will not parse. Matched on the
# message because it raises a plain Error for this, not a distinct type.
_SYNTAX_MARKERS = (
    "unexpected token",
    "unknown engine",
    "malformed",
    "selector parse",
    "invalid selector",
)


def _fault_of(exc: BaseException) -> str:
    text = str(exc).lower()
    if any(marker in text for marker in _SYNTAX_MARKERS):
        return "invalid"
    return "timeout"


class Page(Protocol):
    """Only what this module needs, so the executor is testable without a browser.

    The signatures mirror Playwright's, `timeout` included — this describes an
    API we are calling, not one we are designing, so ASYNC109 is muted here.
    """

    async def click(self, selector: str, *, timeout: float) -> None: ...  # noqa: ASYNC109
    async def fill(self, selector: str, value: str, *, timeout: float) -> None: ...  # noqa: ASYNC109
    async def wait_for_selector(self, selector: str, *, timeout: float) -> Any: ...  # noqa: ASYNC109, E501
    async def wait_for_timeout(self, ms: float) -> None: ...

    # `mouse` and `keyboard` are namespaces on the real Page
    # (page.mouse.wheel, page.keyboard.press), not flat methods.
    mouse: Any
    keyboard: Any

    async def screenshot(self, **kw: Any) -> bytes: ...
    async def evaluate(self, script: str) -> Any: ...
    async def content(self) -> str: ...


class Results:
    """What the sequence produced, in the shape ActionResults already declares."""

    def __init__(self) -> None:
        self.screenshots: list[str] = []
        self.scrapes: list[dict[str, Any]] = []
        self.javascript_returns: list[dict[str, Any]] = []
        self.captcha: list[dict[str, Any]] = []
        self.final_response: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "screenshots": self.screenshots,
            "scrapes": self.scrapes,
            "javascriptReturns": self.javascript_returns,
            "captcha": self.captcha,
        }

    def __bool__(self) -> bool:
        return bool(self.screenshots or self.scrapes or self.javascript_returns or self.captcha)


def _budget(deadline: float) -> float:
    """Milliseconds a step may have: what is left, less the capture reserve."""
    remaining_ms = (deadline - time.monotonic()) * 1000 - CAPTURE_RESERVE_MS
    return max(0.0, min(float(MAX_STEP_MS), remaining_ms))


async def run(
    page: Any,
    steps: list[Any],
    *,
    timeout_ms: int,
    before_result: Callable[[str, float, Results], Awaitable[None]] | None = None,
) -> Results:
    """Perform `steps` against `page`. Raises ActionError on the first failure.

    `steps` are the validated Action models; they are read by attribute, so a
    field the model guarantees is not re-checked here.
    """
    out = Results()
    if not steps:
        return out

    deadline = time.monotonic() + timeout_ms / 1000
    interacted = False
    for index, step in enumerate(steps):
        kind = getattr(step, "type", "?")
        slice_ms = _budget(deadline)
        if slice_ms <= 0:
            raise ActionError(
                index,
                kind,
                "the request ran out of time before this step",
                fault="timeout",
                evidence=out.captcha[-1] if out.captcha else None,
            )
        try:
            if before_result and interacted and kind == "wait" and step.selector:
                step_deadline = time.monotonic() + slice_ms / 1000
                await before_result(step.selector, slice_ms, out)
                slice_ms = min(_budget(deadline), (step_deadline - time.monotonic()) * 1000)
                if slice_ms <= 0:
                    raise TimeoutError("Request budget exhausted while waiting for form results")
            await _one(page, step, kind, slice_ms, out)
            if kind in {"click", "press"}:
                interacted = True
        except ActionError as exc:
            if out.captcha and exc.evidence is None:
                exc.evidence = out.captcha[-1]
            raise
        except TimeoutError as exc:
            raise ActionError(
                index,
                kind,
                (
                    str(exc)
                    if kind == "captchaCheckbox"
                    else f"timed out after {int(slice_ms)}ms — the page did not answer this step"
                ),
                fault="timeout",
                evidence=out.captcha[-1] if out.captcha else None,
            ) from exc
        except Exception as exc:  # noqa: BLE001 - the caller is told which step and why
            raise ActionError(
                index,
                kind,
                f"{type(exc).__name__}: {str(exc)[:300]}",
                fault=_fault_of(exc),
                evidence=getattr(exc, "evidence", None)
                or (out.captcha[-1] if out.captcha else None),
            ) from exc

    logger.info(
        "actions_performed",
        steps=len(steps),
        screenshots=len(out.screenshots),
        scrapes=len(out.scrapes),
        js_returns=len(out.javascript_returns),
    )
    return out


async def run_browser(page: Any, req: Any, *, timeout_ms: int) -> Results:
    """Handle interstitials and challenges blocking requested action results."""
    started = time.monotonic()
    out = Results()
    explicit = any(a.type == "captchaCheckbox" for a in req.actions)
    before_result = None
    if req.captcha_handling == "auto" and not explicit:
        try:
            from engine.core.fetch.captcha_checkbox import CheckboxFailure, automatic
        except ImportError:
            pass  # The public build has no automatic challenge adapter.
        else:

            async def handle_result(selector: str, budget: float, result: Results) -> None:
                try:
                    evidence, response = await automatic(
                        page,
                        budget_ms=budget,
                        state=req.captcha_state,
                        capture=req.captcha_evidence,
                        success_selector=selector,
                    )
                except CheckboxFailure as exc:
                    raise ActionError(
                        -1, "automaticCheckbox", str(exc), fault="challenge", evidence=exc.evidence
                    ) from exc
                if evidence is not None:
                    result.captcha.append(evidence)
                    if response is not None:
                        result.final_response = response

            before_result = handle_result
            try:
                evidence, response = await automatic(
                    page,
                    budget_ms=max(0, timeout_ms - CAPTURE_RESERVE_MS),
                    state=req.captcha_state,
                    capture=req.captcha_evidence,
                )
            except CheckboxFailure as exc:
                raise ActionError(
                    -1, "automaticCheckbox", str(exc), fault="challenge", evidence=exc.evidence
                ) from exc
            if evidence is not None:
                out.captcha.append(evidence)
                out.final_response = response
    try:
        performed = await run(
            page,
            req.actions,
            before_result=before_result,
            timeout_ms=max(0, timeout_ms - int((time.monotonic() - started) * 1000)),
        )
    except ActionError as exc:
        if out.captcha and exc.evidence is None:
            exc.evidence = out.captcha[-1]
        raise
    if explicit and performed.captcha:
        req.captcha_state["attempted"] = True
    performed.captcha = out.captcha + performed.captcha
    if performed.final_response is None:
        performed.final_response = out.final_response
    return performed


async def _one(page: Any, step: Any, kind: str, slice_ms: float, out: Results) -> None:
    if kind == "wait":
        if step.selector is not None:
            await page.wait_for_selector(step.selector, timeout=slice_ms)
        else:
            # Never longer than the step slice: a caller asking to wait a
            # minute inside a thirty-second request is asking for a timeout.
            await page.wait_for_timeout(min(float(step.milliseconds or 0), slice_ms))

    elif kind == "click":
        await page.click(step.selector, timeout=slice_ms)

    elif kind == "captchaCheckbox":
        # Proprietary: the open core accepts the step's shape but cannot run it.
        try:
            from engine.core.fetch.captcha_checkbox import attempt
        except ImportError as exc:
            raise ValueError("captchaCheckbox is not available in this build") from exc

        evidence, response = await attempt(page, step, budget_ms=slice_ms)
        out.captcha.append(evidence)
        if response is not None:
            out.final_response = response

    elif kind == "write":
        # fill() replaces the field's value and fires the events a real edit
        # fires; typing character by character is slower and no more faithful
        # for a form we are filling on purpose.
        await page.fill(step.selector, step.text, timeout=slice_ms)

    elif kind == "press":
        await page.keyboard.press(step.key)

    elif kind == "scroll":
        delta = SCROLL_PIXELS * step.amount * (-1 if step.direction == "up" else 1)
        await page.mouse.wheel(0, delta)
        # Let anything the scroll triggered start loading before the next step.
        await page.wait_for_timeout(min(400.0, slice_ms))

    elif kind == "screenshot":
        shot = await asyncio.wait_for(
            page.screenshot(full_page=step.fullPage, type="jpeg", quality=80),
            timeout=slice_ms / 1000,
        )
        out.screenshots.append("data:image/jpeg;base64," + base64.b64encode(shot).decode())

    elif kind == "scrape":
        html = await asyncio.wait_for(page.content(), timeout=slice_ms / 1000)
        out.scrapes.append({"url": page.url, "html": html})

    elif kind == "executeJavascript":
        value = await asyncio.wait_for(page.evaluate(step.script), timeout=slice_ms / 1000)
        out.javascript_returns.append({"value": _jsonable(value)})

    else:  # pragma: no cover - the model's discriminator forbids this
        raise ActionError(0, kind, "unknown action type")


def _jsonable(value: Any) -> Any:
    """Whatever the page returned, reduced to something that survives JSON.

    A page can hand back a DOM node or a function, which serialise to nothing
    useful; saying so beats a 500 from the response encoder.
    """
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)[:500]
