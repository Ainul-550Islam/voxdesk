// Package status mirrors the Step 13 status vocabulary and exit-code contract
// from app/release/ops.py, so Go ops tooling feeds the release gate the same
// evidence the Python verifiers produce today.
package status

// Operational statuses (Step 13 section O).
const (
	StatusPass    = "PASS"
	StatusFail    = "FAIL"
	StatusBlocked = "BLOCKED"
	StatusNotRun  = "NOT_RUN"
	StatusWaived  = "WAIVED"
	StatusSkipped = "SKIPPED"
	StatusNA      = "NOT_APPLICABLE"
)

// Exit codes (Step 13 section O): 0 = success, 1 = failure, 2 = blocked,
// 3 = invalid configuration.
const (
	ExitOK      = 0
	ExitFail    = 1
	ExitBlocked = 2
	ExitConfig  = 3
)

// ExitCodeForStatus mirrors ops.exit_code_for_status exactly, including the
// fallthrough: WAIVED, empty, and unknown statuses are EXIT_CONFIG (3).
func ExitCodeForStatus(status string) int {
	switch status {
	case StatusPass:
		return ExitOK
	case StatusFail:
		return ExitFail
	case StatusBlocked, StatusNotRun:
		return ExitBlocked
	case StatusSkipped, StatusNA:
		return ExitOK
	default:
		return ExitConfig
	}
}

// WorstExitCode mirrors ops.worst_exit_code: a config error beats a failure,
// which beats blocked; anything else (including an empty set) is success.
func WorstExitCode(codes []int) int {
	seen := make(map[int]bool, len(codes))
	for _, code := range codes {
		seen[code] = true
	}
	for _, candidate := range []int{ExitConfig, ExitFail, ExitBlocked} {
		if seen[candidate] {
			return candidate
		}
	}
	return ExitOK
}
