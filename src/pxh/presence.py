"""One source of truth for "is this tracker home", shared by every consumer.

#305: a bare ``distance_km < 0.15`` threshold decided ``at_home`` in
``pxh.mind._enrich_tracker``, and a *second*, independent bare 0.15 km threshold
decided the "at home" label in ``pxh.voice_loop``'s prompt context. A Chipolo fix
carrying ``gps_accuracy_m: 34`` sitting near that radius crosses it six times an
hour, so one arrival produced six ``person_arrived_home`` transitions (277 in
12.6 days) — and the two consumers could disagree about the same fix.

Hysteresis has to be on the *state*, not on the greeting.
``GREET_ARRIVAL_COOLDOWN_S`` damps speech while leaving every other reader of the
same state oscillating, which is why it is not the fix and why the radii live in
one module instead of in each reader.

Two mechanisms, both required by the evidence:

* separate enter/exit radii — a tracker inside the band keeps the state it
  already had instead of re-deciding on every fix;
* an accuracy gate — a fix whose stated accuracy is worse than the radius it
  would be judged against is not evidence about that radius, so it holds the
  state rather than flipping it.

Deliberately *not* a "N consecutive samples" debounce: ``findmyhub.json`` is
refreshed by an external cron roughly every 5 minutes while the awareness loop
reads it every 60 s, so consecutive *reads* are usually the same sample. A
consecutive-sample rule would have to dedupe on ``ts`` first, and even then it
would delay a genuine arrival by a whole sample period — which the acceptance
criteria for #305 explicitly rule out.
"""

from __future__ import annotations

#: away → home
ENTER_RADIUS_KM = 0.15
#: home → away. Never equal to the enter radius: that is the whole point.
EXIT_RADIUS_KM = 0.30

ENTER_RADIUS_M = ENTER_RADIUS_KM * 1000
EXIT_RADIUS_M = EXIT_RADIUS_KM * 1000

#: Reason tags returned by :func:`latch_at_home`. Callers only need to tell a
#: decision from a refusal, and a refusal worth logging from one that is not.
DECISION_REASONS = ("enter", "exit", "away", "stay")
HOLD_REASONS = ("hold-band", "hold-accuracy", "unknown")
#: The two refusals that mean "a flip was suppressed", i.e. the flapping #305
#: filed. `unknown` is not one of them — it is the first-ever fix being too
#: coarse to promote, which is a different claim.
SUPPRESSED_FLIP_REASONS = ("hold-band", "hold-accuracy")


def latch_at_home(
    distance_km: float,
    accuracy_m: float | None,
    previous: bool | None,
) -> tuple[bool | None, str]:
    """Decide — or decline to decide — whether one tracker fix means "home".

    Returns ``(state, reason)``. ``state is None`` means *no new information*:
    keep whatever the tracker's state already was. That is not the same as
    "away", and collapsing the two is the bug this module exists to prevent.

    ``previous`` is the latched state from the last usable fix (``None`` when
    there has never been one — a daemon restart, and also the reason the first
    read of a tracker can never produce an arrival edge).

    Reasons:

    ``enter`` / ``exit``
        the state moved (``exit`` needs the wider radius).
    ``stay`` / ``away``
        a usable fix that confirms the existing state.
    ``hold-band``
        home, past the enter radius, short of the exit radius. The old bare
        threshold called this "away"; refusing to believe it is the fix.
    ``hold-accuracy``
        the fix is coarser than the radius the applicable test uses, so it is
        not evidence either way.
    ``unknown``
        no previous state *and* no usable fix — nothing to hold.
    """
    accuracy = 0.0 if accuracy_m is None else max(0.0, float(accuracy_m))
    was_home = bool(previous)

    if distance_km <= ENTER_RADIUS_KM and accuracy <= ENTER_RADIUS_M:
        return True, ("stay" if was_home else "enter")
    if distance_km > EXIT_RADIUS_KM and accuracy <= EXIT_RADIUS_M:
        return False, ("away" if not was_home else "exit")

    # Nothing is strong enough to move the state. Name why: a suppressed flip
    # and an ordinary confirmation have to be distinguishable in the evidence.
    if previous is None:
        return None, "unknown"
    if accuracy > (EXIT_RADIUS_M if was_home else ENTER_RADIUS_M):
        return None, "hold-accuracy"
    if was_home:
        return None, "hold-band"
    return None, "away"


def describe_location(distance_km: float) -> str:
    """Prompt-facing label for one fix, in the same bands as the latch.

    The band exists so a jittering tracker reads "near home" instead of
    alternating between "at home" and "0.2km from home" — the second consumer
    #305 named, where the prompt was getting a different answer from the state
    SPARK was acting on.
    """
    if distance_km <= ENTER_RADIUS_KM:
        return "at home"
    if distance_km <= EXIT_RADIUS_KM:
        return "near home"
    return f"{distance_km:.1f}km from home"
