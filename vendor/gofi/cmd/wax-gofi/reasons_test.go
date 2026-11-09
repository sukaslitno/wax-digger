package main

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"

	"github.com/d-fi/GoFi/download"
	"github.com/d-fi/GoFi/types"
)

func TestFailureReason(t *testing.T) {
	cases := []struct {
		name    string
		err     error
		quality string
		want    string
	}{
		{"no file offered", nil, "320", reasonNoFile},
		{"license 320", &download.WrongLicense{Format: "MP3_320"}, "320", "not available in mp3 320 for this account"},
		{"license flac wrapped", fmt.Errorf("get url: %w", &download.WrongLicense{Format: "FLAC"}), "flac", "not available in flac for this account"},
		{"license without format", &download.WrongLicense{}, "128", "not available in mp3 128 for this account"},
		{"geo", &download.GeoBlocked{Country: "DE"}, "320", reasonCountry},
		{"cancelled", fmt.Errorf("download interrupted: %w", context.Canceled), "320", reasonCancelled},
		{"session", errors.New("invalid Deezer user data response: missing license token"), "320", reasonSession},
		{"disk", errors.New("write /x: no space left on device"), "320", reasonDiskFull},
		{"folder", errors.New("open /x: permission denied"), "320", reasonNoWrite},
		{"tags", errors.New("failed to add metadata: bad picture"), "320", reasonTagging},
		{"http", errors.New("download failed: 403 Forbidden"), "320", reasonRefused},
		{"reset", errors.New("read tcp: connection reset by peer"), "320", reasonNetwork},
		{"dns", errors.New("dial tcp: lookup e-cdns-proxy-a.dzcdn.net: no such host"), "320", reasonNetwork},
		{"unknown", errors.New("something odd"), "320", reasonGeneric},
	}
	for _, c := range cases {
		got := failureReason(c.err, c.quality)
		if got != c.want {
			t.Errorf("%s: got %q, want %q", c.name, got, c.want)
		}
		if got != strings.ToLower(got) {
			t.Errorf("%s: reason %q is not lowercase", c.name, got)
		}
	}
}

func TestDetail(t *testing.T) {
	if detail(nil) != "" {
		t.Fatal("nil error must give no detail")
	}
	if got := detail(errors.New("API Error:\n  Bad   Thing")); got != "api error: bad thing" {
		t.Fatalf("got %q", got)
	}
	if got := detail(errors.New(strings.Repeat("x", 400))); len(got) > 310 {
		t.Fatalf("detail is not capped: %d", len(got))
	}
}

func TestMissingPositions(t *testing.T) {
	pos := func(n int) types.TrackType { return types.TrackType{TRACK_POSITION: &n} }
	got := missingPositions([]types.TrackType{pos(1), pos(3), {}, pos(5)}, 5)
	if !reflect.DeepEqual(got, []int{2, 4}) {
		t.Fatalf("got %v", got)
	}
	if got := missingPositions(nil, 0); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}

func TestTrackEvent(t *testing.T) {
	e := trackEvent("failed", "A", "B", reasonNotFound, "")
	if e["type"] != "track" || e["status"] != "failed" || e["artist"] != "A" || e["title"] != "B" || e["reason"] != reasonNotFound {
		t.Fatalf("bad event %v", e)
	}
	if _, ok := e["detail"]; ok {
		t.Fatal("empty detail must be left out")
	}
	if trackEvent("failed", "", "", "x", "why")["detail"] != "why" {
		t.Fatal("detail missing")
	}
}

func TestQualityLabel(t *testing.T) {
	for in, want := range map[string]string{"128": "mp3 128", "MP3_320": "mp3 320", "FLAC": "flac", "flac": "flac"} {
		if got := qualityLabel(in); got != want {
			t.Errorf("%s: got %q want %q", in, got, want)
		}
	}
}
