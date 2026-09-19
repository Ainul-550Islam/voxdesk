import { describe, expect, it } from "vitest";
import {
  formatCurrencyCents,
  formatDateTime,
  formatDays,
  formatDuration,
  formatPhone,
  titleCase,
} from "./format";

describe("formatDuration", () => {
  it("formats sub-minute durations", () => {
    expect(formatDuration(0)).toBe("0s");
    expect(formatDuration(24)).toBe("24s");
  });

  it("formats minutes and seconds", () => {
    expect(formatDuration(84)).toBe("1m 24s");
    expect(formatDuration(3599)).toBe("59m 59s");
  });

  it("formats hours", () => {
    expect(formatDuration(3723)).toBe("1h 2m");
  });

  it("clamps negative input to zero", () => {
    expect(formatDuration(-5)).toBe("0s");
  });
});

describe("formatDateTime", () => {
  it("renders a dash for missing input", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
    expect(formatDateTime("")).toBe("—");
  });

  it("renders a dash for unparseable input", () => {
    expect(formatDateTime("not-a-date")).toBe("—");
  });

  it("renders a readable date for a valid ISO string", () => {
    const out = formatDateTime("2026-09-14T10:00:00Z");
    expect(out).not.toBe("—");
    expect(out.length).toBeGreaterThan(4);
  });
});

describe("formatPhone", () => {
  it("formats 10-digit numbers", () => {
    expect(formatPhone("4155551234")).toBe("(415) 555-1234");
  });

  it("formats 11-digit numbers with a leading country code", () => {
    expect(formatPhone("14155551234")).toBe("+1 (415) 555-1234");
  });

  it("passes through unrecognized shapes", () => {
    expect(formatPhone("1234")).toBe("1234");
  });
});

describe("formatCurrencyCents", () => {
  it("formats cents as dollars", () => {
    expect(formatCurrencyCents(1999)).toMatch(/\$19\.99/);
  });

  it("handles zero", () => {
    expect(formatCurrencyCents(0)).toMatch(/\$0\.00/);
  });
});

describe("titleCase", () => {
  it("turns snake_case into Title Case", () => {
    expect(titleCase("in_progress")).toBe("In Progress");
    expect(titleCase("no_answer")).toBe("No Answer");
  });
});

describe("formatDays", () => {
  it("renders a dash for an empty schedule", () => {
    expect(formatDays([])).toBe("—");
  });

  it("maps day numbers to short names", () => {
    expect(formatDays([1, 3, 5])).toBe("Mon, Wed, Fri");
  });

  it("passes through out-of-range numbers as-is", () => {
    expect(formatDays([0, 7])).toBe("Sun, 7");
  });
});
