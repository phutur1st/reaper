# SPDX-License-Identifier: AGPL-3.0-or-later
"""The optional deletion gate, including callers that bypass plan-time filtering."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from reaper.api.runs import _planned_candidates
from reaper.db.models import ActionStep, Candidate, FirstFlagged, Profile
from reaper.engine.policy import ProfileSettings
from reaper.services.breakdown import reap_breakdown
from reaper.services.executor import Executor, StepOutcome, _Delete
from reaper.services.grace import deletion_eligibility, grace_report
from reaper.services.planner import PlanError, build_plan
from reaper.services.profiles import save_profile_settings

from .test_reap_loop import GB, _read_only, _snapshot_with


@pytest.fixture
async def session(async_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with async_factory() as s:
        yield s


NOW = datetime(2026, 1, 20, 12, tzinfo=UTC)


async def _flag(session: AsyncSession, key: str, age: timedelta) -> None:
    session.add(FirstFlagged(media_key=key, first_flagged_at=NOW - age, last_seen_condemned_at=NOW))
    await session.flush()


@pytest.mark.parametrize("key", ["radarr:1:1", "sonarr:1:1:2"])
@pytest.mark.parametrize(
    "age,allowed",
    [
        (None, False),
        (timedelta(days=-1), False),
        (timedelta(days=21, seconds=-1), False),
        (timedelta(days=21), True),
        (timedelta(days=21, seconds=1), True),
    ],
)
async def test_exact_boundary_and_missing_clock(
    session: AsyncSession,
    key: str,
    age: timedelta | None,
    allowed: bool,
) -> None:
    snap = await _snapshot_with(session, [(key, GB)])
    candidate = (
        await session.execute(select(Candidate).where(Candidate.snapshot_id == snap))
    ).scalar_one()
    if age is not None:
        await _flag(session, key, age)
    result = await deletion_eligibility(
        session,
        {key: candidate},
        ProfileSettings(enforce_grace_period=True, grace_days=21),
        now=NOW,
    )
    assert list(result.eligible) == ([key] if allowed else [])
    assert list(result.waiting) == ([] if allowed else [key])
    # Off preserves the existing deletion set, even with no clock.
    result = await deletion_eligibility(session, {key: candidate}, ProfileSettings(), now=NOW)
    assert list(result.eligible) == [key]
    assert result.waiting == {}


async def test_bulk_plan_counts_and_notice_set_agree(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("reaper.services.grace.utcnow", lambda: NOW)
    monkeypatch.setattr("reaper.services.breakdown.utcnow", lambda: NOW)
    keys = ["radarr:1:1", "radarr:1:2", "radarr:1:3"]
    snap = await _snapshot_with(session, [(k, (i + 1) * GB) for i, k in enumerate(keys)])
    await _flag(session, keys[0], timedelta(days=21))
    await _flag(session, keys[1], timedelta(days=2))
    await save_profile_settings(session, ProfileSettings(enforce_grace_period=True, grace_days=21))
    run = await build_plan(session, snapshot_id=snap, approved_by="test")
    planned = await _planned_candidates(session, run)
    assert [c.media_key for c in planned] == [keys[0]]
    report = await reap_breakdown(session)
    assert report.will_reap == 1
    assert report.will_reap_bytes == GB
    assert len(report.grace_waiting) == 2
    assert report.grace_waiting_bytes == 5 * GB
    assert {c.grace_ends_at for c in report.grace_waiting} == {None, NOW + timedelta(days=19)}
    notices = await grace_report(session, grace_days=21, now=NOW)
    assert {c.media_key for c in notices.in_grace} == set(keys[1:])
    assert {c.media_key for c in notices.ready} == {keys[0]}
    dry = await Executor(
        session,
        safety=_read_only(),
        settings=ProfileSettings(enforce_grace_period=True, grace_days=21),
    ).execute(run.id)
    assert dry.would_delete_items == 1
    assert dry.would_delete_bytes == GB
    assert dry.outcomes[0].media_key == keys[0]
    assert dry.outcomes[0].is_canary
    with pytest.raises(PlanError, match="waiting for grace"):
        await build_plan(session, snapshot_id=snap, approved_by="test", only_media_keys={keys[1]})


@pytest.mark.parametrize("key", ["radarr:1:1", "sonarr:1:1:2"])
@pytest.mark.parametrize("dry_run", [True, False])
async def test_per_item_gate_cannot_be_bypassed_by_a_preexisting_plan(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    dry_run: bool,
) -> None:
    """Call the send entry directly: upstream filtering cannot make this proof vacuous."""
    monkeypatch.setattr("reaper.services.grace.utcnow", lambda: NOW)
    snap = await _snapshot_with(session, [(key, GB)])
    run = await build_plan(session, snapshot_id=snap, approved_by="test")
    candidate = (
        await session.execute(select(Candidate).where(Candidate.snapshot_id == snap))
    ).scalar_one()
    steps = tuple(
        (
            await session.execute(
                select(ActionStep).where(ActionStep.run_id == run.id).order_by(ActionStep.ordinal)
            )
        ).scalars()
    )
    executor = Executor(session, safety=_read_only(), settings=ProfileSettings(), dry_run=dry_run)
    executor._effective_keys = {key}
    movie = AsyncMock()
    season = AsyncMock()
    monkeypatch.setattr(executor, "_send_movie", movie)
    monkeypatch.setattr(executor, "_send_season", season)
    # An old plan had no gate, but live enablement must stop both send paths.
    await save_profile_settings(session, ProfileSettings(enforce_grace_period=True))
    outcome = await executor._one_delete(
        _Delete(steps=steps, candidate=candidate), is_canary=True, approved_at=NOW
    )
    assert outcome.detail.id == "error.reap.step.grace_waiting"
    movie.assert_not_awaited()
    season.assert_not_awaited()
    assert await _planned_candidates(session, run) == []


async def test_live_clock_reset_and_settings_tightening_are_read_fresh(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("reaper.services.grace.utcnow", lambda: NOW)
    key = "radarr:1:1"
    snap = await _snapshot_with(session, [(key, GB)])
    await _flag(session, key, timedelta(days=21))
    await save_profile_settings(session, ProfileSettings(enforce_grace_period=True, grace_days=14))
    candidate = (
        await session.execute(select(Candidate).where(Candidate.snapshot_id == snap))
    ).scalar_one()
    executor = Executor(session, safety=_read_only(), settings=ProfileSettings())
    # Keep stale ORM instances alive while changing their stored values.
    clock = await session.get(FirstFlagged, key)
    profile = (await session.execute(select(Profile))).scalar_one()
    assert clock is not None
    assert await executor._grace_refusal(candidate) is None
    await session.execute(
        update(Profile)
        .values(
            settings_json=ProfileSettings(
                enforce_grace_period=True, grace_days=30
            ).model_dump_json()
        )
        .execution_options(synchronize_session=False)
    )
    assert '"grace_days":14' in profile.settings_json
    refusal = await executor._grace_refusal(candidate)
    assert refusal is not None
    assert refusal.id == "error.reap.step.grace_waiting"
    await session.execute(
        update(Profile)
        .values(
            settings_json=ProfileSettings(
                enforce_grace_period=True, grace_days=14
            ).model_dump_json()
        )
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(FirstFlagged)
        .values(first_flagged_at=NOW)
        .execution_options(synchronize_session=False)
    )
    assert clock.first_flagged_at == NOW - timedelta(days=21)
    refusal = await executor._grace_refusal(candidate)
    assert refusal is not None
    assert refusal.id == "error.reap.step.grace_waiting"


async def test_a_hand_reap_cannot_skip_grace(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from reaper.services import whitelist

    from .test_reap_loop import _clean_explanation

    monkeypatch.setattr("reaper.services.grace.utcnow", lambda: NOW)
    key = "radarr:1:1"
    snap = await _snapshot_with(session, [(key, GB)])
    candidate = (
        await session.execute(select(Candidate).where(Candidate.snapshot_id == snap))
    ).scalar_one()
    candidate.verdict = "protect"
    candidate.explanation_json = _clean_explanation()
    await whitelist.set_override(
        session, media_key=key, title="Example", decision="reap", note=None
    )
    # First prove this hand reap actually reaches the planner with the option off.
    run = await build_plan(session, snapshot_id=snap, approved_by="test")
    assert [c.media_key for c in await _planned_candidates(session, run)] == [key]
    await _flag(session, key, timedelta(days=1))
    await save_profile_settings(session, ProfileSettings(enforce_grace_period=True))
    with pytest.raises(PlanError, match="waiting for grace"):
        await build_plan(session, snapshot_id=snap, approved_by="test", only_media_keys={key})


async def test_mixed_show_selection_refuses_instead_of_silently_dropping_a_season(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("reaper.services.grace.utcnow", lambda: NOW)
    keys = ["sonarr:1:1:1", "sonarr:1:1:2"]
    snap = await _snapshot_with(session, [(k, GB) for k in keys])
    await session.execute(update(Candidate).values(group_key="sonarr:1:1"))
    await _flag(session, keys[0], timedelta(days=30))
    await _flag(session, keys[1], timedelta(days=1))
    await save_profile_settings(session, ProfileSettings(enforce_grace_period=True))
    with pytest.raises(PlanError, match="waiting for grace"):
        await build_plan(
            session, snapshot_id=snap, approved_by="test", only_media_keys={"sonarr:1:1"}
        )
    run = await build_plan(session, snapshot_id=snap, approved_by="test")
    assert [c.media_key for c in await _planned_candidates(session, run)] == [keys[0]]


async def test_profile_round_trip_old_defaults_and_unreadable_settings(
    session: AsyncSession,
) -> None:
    from reaper.services.profiles import active_profile

    assert not (await active_profile(session)).settings.enforce_grace_period
    await save_profile_settings(session, ProfileSettings(enforce_grace_period=True))
    assert (await active_profile(session)).settings.enforce_grace_period
    profile = (await session.execute(select(Profile))).scalar_one()
    old = ProfileSettings().model_dump(exclude={"enforce_grace_period"})
    import json

    profile.settings_json = json.dumps(old)
    await session.flush()
    assert not (await active_profile(session)).settings.enforce_grace_period
    profile.settings_json = '{"enforce_grace_period": "unreadable"}'
    await session.flush()
    snap = await _snapshot_with(session, [("radarr:1:1", GB)])
    with pytest.raises(PlanError, match="couldn't read the limits"):
        await build_plan(session, snapshot_id=snap, approved_by="test")


async def test_claim_time_grace_survives_mid_run_disablement(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("reaper.services.grace.utcnow", lambda: NOW)
    keys = ["radarr:1:1", "radarr:1:2"]
    snap = await _snapshot_with(session, [(keys[0], GB), (keys[1], 2 * GB)])
    for key in keys:
        await _flag(session, key, timedelta(days=30))
    # The route read the defaults; grace was enabled before the executor claimed the run.
    run = await build_plan(session, snapshot_id=snap, approved_by="test")
    await save_profile_settings(session, ProfileSettings(enforce_grace_period=True, grace_days=21))
    executor = Executor(session, safety=_read_only(), settings=ProfileSettings())
    original = executor._one_delete

    async def reset_before_second(
        delete: _Delete, *, is_canary: bool, approved_at: datetime
    ) -> StepOutcome:
        if delete.candidate.media_key == keys[1]:
            await save_profile_settings(session, ProfileSettings(enforce_grace_period=False))
            await session.execute(
                update(FirstFlagged)
                .where(FirstFlagged.media_key == keys[1])
                .values(first_flagged_at=NOW)
            )
        return await original(delete, is_canary=is_canary, approved_at=approved_at)

    monkeypatch.setattr(executor, "_one_delete", reset_before_second)
    report = await executor.execute(run.id)
    assert report.would_delete_items == 1
    assert report.would_delete_bytes == GB
    assert report.skipped == 1
    assert report.outcomes[1].detail.id == "error.reap.step.grace_waiting"
