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
  state rather than flipping it;
* two agreeing fixes before the state may leave home (``EXIT_CONFIRMATIONS``).
  Arrivals are single-sample on purpose: waiting on the side that greets would
  trade a spurious greeting for a late one, which is not a trade this system
  makes.

Deliberately *not* a "N consecutive samples" debounce: ``findmyhub.json`` is
refreshed by an external cron roughly every 5 minutes while the awareness loop
reads it every 60 s, so consecutive *reads* are usually the same sample. A
consecutive-sample rule would have to dedupe on ``ts`` first, and even then it
would delay a genuine arrival by a whole sample period — which the acceptance
criteria for #305 explicitly rule out.
"""

from __future__ import annotations

#: The decided claim about one tracker, as a name rather than a boolean.
#:
#: ``at_home`` used to be a bare ``True``/``False`` — and, crucially, *absent*
#: meant a third thing nobody had named. Two readers then disagreed about the
#: same name: the coordinate branch published a dict with no ``at_home`` at all,
#: while the semantic-address branch published its own boolean derived from an
#: address string. When one tracker's representation alternated between those two
#: shapes, the arrival detector compared a coordinate claim against a semantic
#: claim and manufactured ``UNKNOWN → HOME`` edges that it read as arrivals
#: (measured on the robot 2026-09-21: 17 false ``person_arrived_home`` for a
#: tracker that never left, every one of them asserted off a 23.95 h old fix
#: 4.01 km away that the staleness gate had just refused as evidence).
#:
#: Naming the third state is the fix. ``AWAY`` requires positive evidence of
#: absence; absence of evidence is ``UNKNOWN``, and ``UNKNOWN`` can never be the
#: far side of an arrival (issue #305).
AT_HOME = "HOME"
AWAY = "AWAY"
UNKNOWN = "UNKNOWN"
#: Valid values for the latched state. A consumer that sees anything else has a
#: bug, not a tracker.
AT_HOME_STATES = (AT_HOME, AWAY, UNKNOWN)
#: Representations that may be compared to each other as evidence about the same
#: physical fact. Two coordinate fixes are comparable because distance is a
#: property of the place; a semantic address and a coordinate are not comparable
#: at all, and a change of representation is not movement.
COORDINATE = "coordinate"
SEMANTIC = "semantic"
#: ``UNKNOWN`` carries no kind: it is the absence of a claim, so there is nothing
#: for a later fix to be compared against.
N_KINDS = (COORDINATE, SEMANTIC)


def normalise_state(state) -> str | None:
    """Read a state that may be written in the pre-#305 boolean dialect.

    ``True``/``False`` were the old encoding of "at home" / "not at home", and
    the arrival detector's documented contract (#156) is still to be handed
    already-formed states. Accepting them here — in one place, named as a
    migration rather than left implicit — is what keeps a boolean from being read
    as a third, *unnamed* value by consumers that never learned about ``UNKNOWN``.
    Anything that is not a recognised state returns ``None``, i.e. no claim.
    """
    if state is True:
        return AT_HOME
    if state is False:
        return AWAY
    if state in AT_HOME_STATES:
        return state
    return None


def arrival_edge(
    previous: str | None,
    previous_kind: str | None,
    current: str | None,
    current_kind: str | None,
) -> bool:
    """Is ``(previous, previous_kind) → (current, current_kind)`` an arrival?

    The whole acceptance criterion for #305 lives here, in one place, because the
    bug was two readers deciding separately:

    * ``previous`` must be a *known* ``AWAY`` — ``UNKNOWN → HOME`` is not an
      arrival, it is a first sighting, and treating it as one is what greeted a
      tracker that had never been seen away (the daemon-restart guard);
    * both sides must be *comparable* evidence — a coordinate fix and a semantic
      address are different kinds of claim about the same name, and a change of
      representation is not movement, so the pairing is refused rather than
      treated as an edge.
    """
    previous, current = normalise_state(previous), normalise_state(current)
    if previous != AWAY or current != AT_HOME:
        return False
    if previous_kind not in N_KINDS or current_kind not in N_KINDS:
        return False
    return previous_kind == current_kind


#: away → home
ENTER_RADIUS_KM = 0.15
#: home → away. Never equal to the enter radius: that is the whole point.
EXIT_RADIUS_KM = 0.30
#: Usable fixes that must agree before the state may leave home (or latch away
#: on a first sighting). One far fix is a claim about one GPS sample: on
#: 2026-09-17 a tracker sitting at home produced one at ±100 m accuracy, the
#: restart guard latched it as away, and the next good fix greeted the arrival —
#: a spurious greeting produced by a deploy. Arrivals stay single-sample: this
#: confirmation is only on the side where waiting costs nothing.
EXIT_CONFIRMATIONS = 2

ENTER_RADIUS_M = ENTER_RADIUS_KM * 1000
EXIT_RADIUS_M = EXIT_RADIUS_KM * 1000

#: Reason tags returned by :func:`latch_at_home`. Callers only need to tell a
#: decision from a refusal, and a refusal worth logging from one that is not.
DECISION_REASONS = ("enter", "exit", "away", "stay")
HOLD_REASONS = ("hold-band", "hold-accuracy", "unknown")
#: Reasons that leave the state unchanged without refusing anything.
NEUTRAL_REASONS = ("pending-far",)
#: A usable far fix that is waiting for its confirmation. Not a state change and
#: not a refusal: the state simply has not moved yet.
PENDING_REASON = "pending-far"
#: The two refusals that mean "a flip was suppressed", i.e. the flapping #305
#: filed. `unknown` is not one of them — it is the first-ever fix being too
#: coarse to promote, which is a different claim.
SUPPRESSED_FLIP_REASONS = ("hold-band", "hold-accuracy")


def latch_at_home(
    distance_km: float,
    accuracy_m: float | None,
    previous: bool | None,
    far_streak: int = 0,
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
    ``pending-far``
        a usable far fix waiting for ``EXIT_CONFIRMATIONS``: no state change.
        `far_streak` is how many consecutive usable far fixes the caller has
        already seen, because only the caller can count across reads.
    """
    accuracy = 0.0 if accuracy_m is None else max(0.0, float(accuracy_m))
    was_home = bool(previous)

    if distance_km <= ENTER_RADIUS_KM and accuracy <= ENTER_RADIUS_M:
        return True, ("stay" if was_home else "enter")
    if distance_km > EXIT_RADIUS_KM and accuracy <= EXIT_RADIUS_M:
        if far_streak + 1 < EXIT_CONFIRMATIONS:
            return None, PENDING_REASON
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
