// SPDX-License-Identifier: AGPL-3.0-or-later
import { useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import type { Candidate, GroupSeasonMark } from "../api";
import { date, time } from "../format";
import { laneOf } from "./reviewFate";

type GraceItem = Pick<
  Candidate,
  "verdict" | "override" | "override_effective" | "grace_enforced" | "grace_ends_at"
>;
const listeners = new Set<() => void>();
let timer: ReturnType<typeof setInterval> | undefined;
let instant = Date.now();
function subscribe(listener: () => void) {
  listeners.add(listener);
  if (timer === undefined) {
    instant = Date.now();
    timer = setInterval(() => {
      instant = Date.now();
      listeners.forEach((notify) => notify());
    }, 1000);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      clearInterval(timer);
      timer = undefined;
    }
  };
}
const snapshot = () => instant;
export function useGraceNow() {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

export function GraceBadge({ item, exact = false }: { item: GraceItem; exact?: boolean }) {
  const { t } = useTranslation();
  const now = useGraceNow();
  if (laneOf(item) !== "condemn") return null;
  if (item.grace_enforced == null) {
    return <span className="chip grace-badge">{t("reviewGrace.unavailable")}</span>;
  }
  const mode = item.grace_enforced ? "grace" : "notice";
  const end = item.grace_ends_at ? Date.parse(item.grace_ends_at) : NaN;
  const remaining = end - now;
  const key = !Number.isFinite(end)
    ? "missing"
    : remaining <= 0
      ? "complete"
      : remaining < 86400000
        ? "soon"
        : "days";
  const deadline =
    Number.isFinite(end) && item.grace_ends_at
      ? t("reviewGrace.deadline", {
          date: date(item.grace_ends_at),
          time: time(item.grace_ends_at),
        })
      : undefined;
  const n = Number.isFinite(remaining) ? Math.ceil(remaining / 86400000) : 0;
  const labels = {
    grace: {
      days: t("reviewGrace.grace.days", { n }),
      soon: t("reviewGrace.grace.soon"),
      complete: t("reviewGrace.grace.complete"),
      missing: t("reviewGrace.grace.missing"),
    },
    notice: {
      days: t("reviewGrace.notice.days", { n }),
      soon: t("reviewGrace.notice.soon"),
      complete: t("reviewGrace.notice.complete"),
      missing: t("reviewGrace.notice.missing"),
    },
  };
  return (
    <span className="chip grace-badge" title={deadline}>
      {labels[mode][key]}
      {exact && deadline && <span className="grace-exact">{deadline}</span>}
    </span>
  );
}

export function GraceSummary({ seasons }: { seasons: ReadonlyArray<GroupSeasonMark | Candidate> }) {
  const { t } = useTranslation();
  const now = useGraceNow();
  const relevant = seasons.filter((s) => laneOf(s) === "condemn");
  if (!relevant.length) return null;
  if (relevant.some((s) => s.grace_enforced == null)) {
    return <span className="chip grace-badge">{t("reviewGrace.unavailable")}</span>;
  }
  const mode = relevant[0]?.grace_enforced ? "grace" : "notice";
  const waiting = relevant.filter(
    (s) =>
      !s.grace_ends_at ||
      !Number.isFinite(Date.parse(s.grace_ends_at)) ||
      Date.parse(s.grace_ends_at) > now,
  ).length;
  return (
    <span className="chip grace-badge">
      {mode === "grace"
        ? t("reviewGrace.grace.summary", { n: waiting, complete: relevant.length - waiting })
        : t("reviewGrace.notice.summary", { n: waiting, complete: relevant.length - waiting })}
    </span>
  );
}
