package status

import "testing"

func TestExitCodeForStatus(t *testing.T) {
	cases := []struct {
		name   string
		status string
		want   int
	}{
		{"pass", StatusPass, ExitOK},
		{"fail", StatusFail, ExitFail},
		{"blocked", StatusBlocked, ExitBlocked},
		{"not_run", StatusNotRun, ExitBlocked},
		{"skipped_is_success", StatusSkipped, ExitOK},
		{"not_applicable_is_success", StatusNA, ExitOK},
		// ops.exit_code_for_status has no WAIVED branch: it falls through
		// to EXIT_CONFIG. Mirror that exactly.
		{"waived_falls_through_to_config", StatusWaived, ExitConfig},
		{"empty_falls_through_to_config", "", ExitConfig},
		{"unknown_falls_through_to_config", "BOGUS", ExitConfig},
		{"lowercase_pass_is_unknown", "pass", ExitConfig},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := ExitCodeForStatus(tc.status); got != tc.want {
				t.Fatalf("ExitCodeForStatus(%q) = %d, want %d", tc.status, got, tc.want)
			}
		})
	}
}

func TestWorstExitCode(t *testing.T) {
	cases := []struct {
		name  string
		codes []int
		want  int
	}{
		{"empty_is_ok", nil, ExitOK},
		{"all_ok", []int{ExitOK, ExitOK}, ExitOK},
		{"fail_beats_ok", []int{ExitOK, ExitFail}, ExitFail},
		{"blocked_beats_ok", []int{ExitBlocked, ExitOK}, ExitBlocked},
		{"fail_beats_blocked", []int{ExitBlocked, ExitFail}, ExitFail},
		{"config_beats_everything", []int{ExitFail, ExitBlocked, ExitOK, ExitConfig}, ExitConfig},
		{"unknown_code_is_ignored", []int{ExitOK, 99}, ExitOK},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := WorstExitCode(tc.codes); got != tc.want {
				t.Fatalf("WorstExitCode(%v) = %d, want %d", tc.codes, got, tc.want)
			}
		})
	}
}
