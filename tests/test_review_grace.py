# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review deadlines agree across list, strip, group and candidate detail responses."""

from datetime import timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from reaper.api.review import candidate_detail, group_detail, list_candidates
from reaper.api.schemas import CandidateOut, GroupSeasonMarkOut
from reaper.db.models import Candidate, Profile
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
