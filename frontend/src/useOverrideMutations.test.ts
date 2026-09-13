// SPDX-License-Identifier: AGPL-3.0-or-later
// The overlay contract for a hand decision, from the mutation's side:
//   - a PER-ITEM decision (movie/season already on screen) is patched by media_key, and the
//     active tab is NOT refetched, so the just-decided row stays put and re-buckets on the
//     next fetch;
//   - a WHOLE-SHOW decision keys on the show/group key: it patches the show-level fields on the
//     group's loaded seasons and, likewise, does NOT refetch the active tab, so the card
//     carrying the control reflects the decision and the show stays in the lane the operator is
//     looking at (a whole-show reap must not re-bucket a Limbo show to Condemned and vanish
//     mid-review);
//   - a decision on a row/show that is NOT loaded matches nothing and falls back to a real
//     refetch, since there is no on-screen overlay to preserve.
// This test guards against both the whole-show "no feedback" gap and the whole-show "jumps
// out of the list" bug.

import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { testQueryClient } from "./test/queryClient";
import { renderHookWithProviders } from "./test/renderWithProviders";
import { api, type Candidate, type CandidatePage } from "./api";
import type { InfiniteData } from "@tanstack/react-query";
import { useOverrideMutations } from "./useOverrideMutations";
import type { QueryClient } from "@tanstack/react-query";

vi.mock("./api", () => ({
  api: {
    override: vi.fn().mockResolvedValue({}),
    clearOverride: vi.fn().mockResolvedValue({}),
    candidate: vi.fn().mockRejectedValue(new Error("Not loaded")),
    group: vi.fn().mockRejectedValue(new Error("Not loaded")),
  },
}));

function movie(n: number, extra: Partial<Candidate> = {}): Candidate {
  const c: Candidate = {
    id: n,
    media_key: `radarr:1:${n}`,
    title: `Example Movie ${n}`,
    media_type: "movie",
    size_bytes: 1024 ** 3,
    verdict: "condemn",
    score: 80,
    coverage_bp: 10_000,
    first_flagged_at: null,
    year: 2011,
    summary: null,
    poster_url: null,
    requested_by: null,
    group_key: null,
    group_title: null,
    video_resolution: null,
    library: null,
    dormant_days: null,
    override: null,
    override_own: null,
    show_override: null,
    override_effective: null,
    spare_expires_at: null,
    spare_covers_until: null,
    show_spare_expires_at: null,
    chip: null,
    show_status: null,
    season_number: null,
    collections: null,
    ...extra,
  };
  // Default an item's own decision to its effective one unless a test sets them apart (to
  // exercise a season kept only by its show).
  if (extra.override_own === undefined) c.override_own = c.override;
  return c;
}

const row = (media_key: string, group_key: string | null) =>
  movie(group_key ? 2 : 1, { media_key, group_key });

const seedCandidates = (client: QueryClient) =>
  client.setQueryData(["candidates", "condemn", "", {}], {
    pages: [{ items: [row("radarr:1:1", null), row("sonarr:1:9:2", "sonarr:1:9")], groups: [] }],
    pageParams: [0],
  });

const cachedRow = (client: QueryClient, key: string) => {
  const data = client.getQueryData(["candidates", "condemn", "", {}]) as {
    pages: {
      items: { media_key: string; override: string | null; show_override: string | null }[];
    }[];
  };
  return data.pages[0]!.items.find((c) => c.media_key === key)!;
};

// The refetchType the queue was invalidated with on the last call touching ["candidates"].
const queueRefetchType = (spy: ReturnType<typeof vi.spyOn>) => {
  const calls = spy.mock.calls as unknown[][];
  const queueCalls = calls.filter(
    (c) => (c[0] as { queryKey?: unknown[] })?.queryKey?.[0] === "candidates",
  );
  return (queueCalls.at(-1)?.[0] as { refetchType?: string })?.refetchType;
};

const setup = () => {
  const client = testQueryClient();
  seedCandidates(client);
  const invalidateSpy = vi.spyOn(client, "invalidateQueries");
  const hook = renderHookWithProviders(() => useOverrideMutations(), { client });
  return { client, invalidateSpy, hook };
};

describe("useOverrideMutations", () => {
  beforeEach(() => vi.clearAllMocks());

  it("patches a per-item decision in place and does NOT refetch the active tab", async () => {
    const { client, invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.setOverride.mutateAsync({ key: "radarr:1:1", decision: "spare" });
    });
    // The row was patched where it sits, keeping it on screen.
    expect(cachedRow(client, "radarr:1:1").override).toBe("spare");
    // ...and the active queue was left alone, so it stays put.
    expect(queueRefetchType(invalidateSpy)).toBe("none");
  });

  it("patches the show-level overlay on a whole-show decision and does NOT refetch", async () => {
    const { client, invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.setOverride.mutateAsync({ key: "sonarr:1:9", decision: "spare" });
    });
    // The group key matches the season's group_key, so the SHOW-LEVEL field is patched...
    expect(cachedRow(client, "sonarr:1:9:2").show_override).toBe("spare");
    // ...while the season's OWN override is untouched (the whole-show decision inherits down).
    expect(cachedRow(client, "sonarr:1:9:2").override).toBeNull();
    // ...and the active queue is NOT refetched, so the show stays in its current lane.
    expect(queueRefetchType(invalidateSpy)).toBe("none");
  });

  it("drops the show-level overlay on a whole-show clear and does NOT refetch", async () => {
    const { client, invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.setOverride.mutateAsync({ key: "sonarr:1:9", decision: "reap" });
    });
    expect(cachedRow(client, "sonarr:1:9:2").show_override).toBe("reap");
    await act(async () => {
      await hook.result.current.clearOverride.mutateAsync("sonarr:1:9");
    });
    // The show-level overlay is gone, and the show still stays put (settles on next fetch).
    expect(cachedRow(client, "sonarr:1:9:2").show_override).toBeNull();
    expect(queueRefetchType(invalidateSpy)).toBe("none");
  });

  it("refetches the active tab when clearing an override on a not-loaded row", async () => {
    const { invalidateSpy, hook } = setup();
    await act(async () => {
      await hook.result.current.clearOverride.mutateAsync("radarr:1:404");
    });
    expect(api.clearOverride).toHaveBeenCalledWith("radarr:1:404");
    expect(queueRefetchType(invalidateSpy)).toBe("active");
  });

  // The scan summary shifts lanes by the overrides, and the Scales figures count only the
  // effective reap set. A spare that does not reach them would leave Jobs still counting the
  // title as reclaimable, and that person still carrying its weight.
  it.each([["a single decision"], ["a bulk decision"]])(
    "refreshes every override-aware surface after %s",
    async (which) => {
      const { invalidateSpy, hook } = setup();
      await act(async () => {
        await hook.result.current.setOverride.mutateAsync({ key: "radarr:1:1", decision: "spare" });
        if (which === "a bulk decision") hook.result.current.refresh();
      });
      const invalidated = (invalidateSpy.mock.calls as unknown[][]).map(
        (c) => (c[0] as { queryKey: unknown[] }).queryKey[0],
      );
      for (const key of [
        "candidates",
        "group",
        "candidate",
        "reap-breakdown",
        "snapshot",
        "fairness",
      ])
        expect(invalidated).toContain(key);
    },
  );
});

it("replaces an elapsed movie clock after sparing and clearing without moving its row", async () => {
  const { client, hook } = setup();
  const cacheKey = ["candidates", "condemn", "", {}];
  const read = () =>
    client.getQueryData<InfiniteData<CandidatePage>>(cacheKey)!.pages[0]!.items[0]!;
  const old = { ...read(), grace_enforced: true, grace_ends_at: "2026-01-01T00:00:00Z" };
  client.setQueryData(cacheKey, { pages: [{ items: [old], groups: [] }], pageParams: [0] });
  vi.mocked(api.candidate).mockResolvedValue({ ...old, grace_ends_at: null } as Awaited<
    ReturnType<typeof api.candidate>
  >);
  await act(async () => {
    await hook.result.current.setOverride.mutateAsync({ key: old.media_key, decision: "spare" });
  });
  const fresh = "2026-10-01T00:00:00Z";
  vi.mocked(api.candidate).mockResolvedValue({ ...old, grace_ends_at: fresh } as Awaited<
    ReturnType<typeof api.candidate>
  >);
  await act(async () => {
    await hook.result.current.clearOverride.mutateAsync(old.media_key);
  });
  expect(read().id).toBe(old.id);
  expect(read().grace_ends_at).toBe(fresh);
  expect(read().grace_enforced).toBe(true);
});

it("replaces clocks in loaded seasons and off-page strip marks after a show decision", async () => {
  const { client, hook } = setup();
  const one = row("sonarr:1:9:2", "sonarr:1:9");
  const two = { ...one, id: 3, media_key: "sonarr:1:9:3" };
  const cacheKey = ["candidates", "condemn", "", {}];
  client.setQueryData(cacheKey, {
    pages: [
      {
        items: [one],
        groups: [
          {
            group_key: one.group_key,
            seasons: [
              { ...one, grace_enforced: true, grace_ends_at: "2020-01-01T00:00:00Z" },
              { ...two, grace_enforced: true, grace_ends_at: "2020-01-01T00:00:00Z" },
            ],
          },
        ],
      },
    ],
    pageParams: [0],
  });
  const fresh = "2026-10-01T00:00:00Z";
  vi.mocked(api.group).mockResolvedValue({
    seasons: [one, two].map((c) => ({
      ...c,
      grace_enforced: true,
      grace_ends_at: fresh,
      spare_expires_at: fresh,
      spare_covers_until: fresh,
      override: "spare",
      override_effective: true,
    })),
  } as Awaited<ReturnType<typeof api.group>>);
  await act(async () => {
    await hook.result.current.setOverride.mutateAsync({
      key: "sonarr:1:9",
      decision: "spare",
      spareDays: 7,
    });
  });
  const page = client.getQueryData<InfiniteData<CandidatePage>>(cacheKey)!.pages[0]!;
  expect(page.items).toHaveLength(1);
  expect(page.items[0]!.grace_ends_at).toBe(fresh);
  expect(page.groups[0]!.seasons.map((c) => c.grace_ends_at)).toEqual([fresh, fresh]);
  expect(page.groups[0]!.seasons.map((c) => c.spare_covers_until)).toEqual([fresh, fresh]);
});

it("clears a stale completion claim when the refreshed clock cannot be read", async () => {
  const { client, hook } = setup();
  vi.mocked(api.candidate).mockRejectedValue(new Error("Unavailable"));
  await act(async () => {
    await hook.result.current.clearOverride.mutateAsync("radarr:1:1");
  });
  const page = client.getQueryData<InfiniteData<CandidatePage>>(["candidates", "condemn", "", {}])!
    .pages[0]!;
  expect(page.items[0]!.grace_enforced).toBeNull();
  expect(page.items[0]!.grace_ends_at).toBeNull();
});
