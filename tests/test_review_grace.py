# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review deadlines agree across list, strip, group and candidate detail responses."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from reaper.api.review import candidate_detail, group_detail, list_candidates
from reaper.api.schemas import CandidateOut, GroupSeasonMarkOut
from reaper.db.models import Candidate, FirstFlagged, Profile
from reaper.engine.policy import ProfileSettings
from reaper.services.grace import deletion_eligibility
from reaper.services.profiles import save_profile_settings

from .test_grace_gate import NOW, _flag
from .test_reap_loop import GB, _snapshot_with


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("days", [21, 35])
async def test_review_surfaces_share_the_gate_deadline(
    async_factory: async_sessionmaker[AsyncSession],
    enabled: bool,
    days: int,
) -> None:
    key = "sonarr:1:1:2"
    async with async_factory() as session:
        snap = await _snapshot_with(session, [(key, GB)])
        await session.execute(update(Candidate).values(group_key="sonarr:1:1"))
        await _flag(session, key, timedelta(days=2))
        settings = ProfileSettings(enforce_grace_period=enabled, grace_days=days)
        await save_profile_settings(session, settings)
        candidate = (
            await session.execute(select(Candidate).where(Candidate.snapshot_id == snap))
        ).scalar_one()
        candidate_id = candidate.id
        boundary = NOW + timedelta(days=days - 2)
        before = await deletion_eligibility(
            session, {key: candidate}, settings, now=boundary - timedelta(seconds=1)
        )
        assert (key in before.waiting) is enabled
        assert (
            key
            in (
                await deletion_eligibility(session, {key: candidate}, settings, now=boundary)
            ).eligible
        )
        await session.commit()
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=async_factory))),
    )
    page = await list_candidates(request, limit=100, offset=0)
    group = await group_detail(request, "sonarr:1:1")
    detail = await candidate_detail(request, candidate_id)
    assert page.total == 1  # Waiting items stay in Review.
    surfaces: list[CandidateOut | GroupSeasonMarkOut] = [
        page.items[0],
        page.groups[0].seasons[0],
        group.seasons[0],
        detail,
    ]
    for item in surfaces:
        assert item.grace_enforced is enabled
        assert item.grace_ends_at == boundary.isoformat()


async def test_review_marks_unreadable_grace_settings_unknown(
    async_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with async_factory() as session:
        await _snapshot_with(session, [("radarr:1:1", GB)])
        await save_profile_settings(session, ProfileSettings(enforce_grace_period=True))
        await session.execute(update(Profile).values(settings_json="not json"))
        await session.commit()
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=async_factory))),
    )
    page = await list_candidates(request, limit=100, offset=0)
    assert page.items[0].grace_enforced is None
    assert page.items[0].grace_ends_at is None


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("days", [21, 35])
async def test_grace_filters_count_before_paging_and_keep_matching_season_scope(
    async_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    days: int,
) -> None:
    monkeypatch.setattr("reaper.api.review.utcnow", lambda: NOW)
    keys = [f"sonarr:1:1:{i}" for i in range(1, 5)]
    async with async_factory() as session:
        await _snapshot_with(session, [(key, (i + 1) * GB) for i, key in enumerate(keys)])
        await session.execute(update(Candidate).values(group_key="sonarr:1:1", library_title="TV"))
        await _flag(session, keys[0], timedelta(days=days))
        await _flag(session, keys[1], timedelta(days=days, seconds=-1))
        await _flag(session, keys[2], timedelta(days=days, seconds=1))
        await save_profile_settings(
            session, ProfileSettings(enforce_grace_period=enabled, grace_days=days)
        )
        await session.commit()
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=async_factory))),
    )
    complete = await list_candidates(
        request, grace_status="complete", library="TV", limit=1, offset=0
    )
    assert complete.total == 2
    assert complete.total_bytes == 4 * GB
    assert complete.grace_enforced is enabled
    assert set(complete.groups[0].matching_keys or []) == {keys[0], keys[2]}
    assert len(complete.groups[0].seasons) == 4
    next_page = await list_candidates(
        request, grace_status="complete", library="TV", limit=1, offset=1
    )
    assert {complete.items[0].media_key, next_page.items[0].media_key} == {keys[0], keys[2]}
    waiting = await list_candidates(request, grace_status="waiting", limit=1, offset=0)
    assert [item.media_key for item in waiting.items] == [keys[1]]
    missing = await list_candidates(request, grace_status="unavailable", limit=1, offset=0)
    assert [item.media_key for item in missing.items] == [keys[3]]
    empty = await list_candidates(
        request, grace_status="complete", library="Other", limit=1, offset=0
    )
    assert empty.total == 0 and empty.total_bytes == 0
    monkeypatch.setattr("reaper.api.review.utcnow", lambda: NOW + timedelta(seconds=1))
    expired = await list_candidates(request, grace_status="complete", limit=100, offset=0)
    assert expired.total == 3


@pytest.mark.parametrize("broken", ["profile", "overflow", "huge_window"])
async def test_unreliable_grace_cannot_match_complete(
    async_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    broken: str,
) -> None:
    monkeypatch.setattr("reaper.api.review.utcnow", lambda: NOW)
    async with async_factory() as session:
        await _snapshot_with(session, [("radarr:1:1", GB)])
        await _flag(session, "radarr:1:1", timedelta(days=50))
        await save_profile_settings(
            session, ProfileSettings(grace_days=10**12 if broken == "huge_window" else 21)
        )
        if broken == "profile":
            await session.execute(update(Profile).values(settings_json="not json"))
        if broken == "overflow":
            await session.execute(
                update(FirstFlagged).values(
                    first_flagged_at=datetime.max.replace(tzinfo=UTC, microsecond=0)
                )
            )
        await session.commit()
    request = cast(
        Request,
        SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=async_factory))),
    )
    complete = await list_candidates(request, grace_status="complete", limit=100, offset=0)
    missing = await list_candidates(request, grace_status="unavailable", limit=100, offset=0)
    assert complete.total == 0
    assert missing.total == 1
