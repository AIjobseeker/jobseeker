package ratelimit

import "time"

// makeTimer returns a channel that receives after d duration.
// Extracted so it can be replaced in tests.
var makeTimer = func(d time.Duration) <-chan time.Time {
	return time.After(d)
}
