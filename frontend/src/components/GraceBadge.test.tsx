// SPDX-License-Identifier: AGPL-3.0-or-later
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { Candidate } from "../api";
import { expectNoA11yViolations } from "../test/a11y";
import { GraceBadge, GraceSummary } from "./GraceBadge";
import { displayFate, laneOf } from "./reviewFate";

const NOW = new Date("2026-01-20T12:00:00Z");
const item = {
  verdict: "condemn",
  override: "reap",
  override_effective: true,
  grace_enforced: true,
  grace_ends_at: "2026-01-25T12:00:00Z",
} satisfies Pick<
  Candidate,
  "verdict" | "override" | "override_effective" | "grace_enforced" | "grace_ends_at"
>;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it.each([
  [true, "2026-01-25T12:00:00Z", "Grace: 5 days left"],
  [true, "2026-01-21T12:00:00Z", "Grace: 1 day left"],
  [true, "2026-01-20T12:00:01Z", "Grace: less than 1 day left"],
  [true, "2026-01-20T12:00:00Z", "Grace complete"],
  [true, null, "Countdown missing, held"],
  [true, "invalid", "Countdown missing, held"],
  [false, "2026-01-25T12:00:00Z", "Notice: 5 days left"],
  [false, "2026-01-20T12:00:01Z", "Notice: less than 1 day left"],
  [false, "2026-01-20T12:00:00Z", "Notice complete"],
  [false, null, "Notice countdown missing"],
  [null, null, "Grace status unavailable, check Pace and limits."],
])("renders mode %s and deadline %s honestly", (grace_enforced, grace_ends_at, expected) => {
  render(<GraceBadge item={{ ...item, grace_enforced, grace_ends_at }} />);
  expect(screen.getByText(expected)).toBeInTheDocument();
});

it("crosses the deadline without a reload, never showing zero days before expiry", () => {
  render(<GraceBadge item={{ ...item, grace_ends_at: "2026-01-20T12:00:01Z" }} />);
  expect(screen.getByText("Grace: less than 1 day left")).toBeInTheDocument();
  act(() => {
    vi.advanceTimersByTime(1000);
  });
  expect(screen.getByText("Grace complete")).toBeInTheDocument();
});

it("shows exact dates in detail views and no countdown on a spared item", () => {
  const view = render(<GraceBadge item={item} exact />);
  expect(screen.getByText(/Countdown ends/)).toBeInTheDocument();
  view.rerender(<GraceBadge item={{ ...item, override: "spare" }} />);
  expect(view.container).toBeEmptyDOMElement();
});

it("keeps waiting hand reaps in Condemned while painting their hold", () => {
  expect(laneOf(item)).toBe("condemn");
  expect(displayFate(item)).toBe("refused");
  expect(displayFate({ ...item, grace_enforced: false })).toBe("reap");
  expect(displayFate({ ...item, grace_ends_at: NOW.toISOString() })).toBe("reap");
});

it("summarizes all marked seasons, including a missing clock, excluding spares", () => {
  const seasons = [
    { ...item, id: 1, season: 1, size_bytes: 1, spare_expires_at: null, spare_covers_until: null },
    {
      ...item,
      id: 2,
      season: 2,
      size_bytes: 1,
      spare_expires_at: null,
      spare_covers_until: null,
      grace_ends_at: null,
    },
    {
      ...item,
      id: 3,
      season: 3,
      size_bytes: 1,
      spare_expires_at: null,
      spare_covers_until: null,
      grace_ends_at: NOW.toISOString(),
    },
  ];
  render(<GraceSummary seasons={[...seasons, { ...seasons[0]!, id: 4, override: "spare" }]} />);
  expect(screen.getByText("2 seasons waiting for grace, 1 grace complete")).toBeInTheDocument();
});

it("keeps the countdown accessible in its exact-date form", async () => {
  vi.useRealTimers();
  const { container } = render(<GraceBadge item={item} exact />);
  await expectNoA11yViolations(container);
});
