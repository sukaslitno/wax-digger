package main

import (
	"context"
	"errors"
	"fmt"
	"strings"

	"github.com/d-fi/GoFi/download"
	"github.com/d-fi/GoFi/types"
)

// Reasons are shown to the user as is: short, lowercase, no jargon.
const (
	reasonAlreadyThere = "already there"
	reasonNotFound     = "not found on deezer"
	reasonNoDetails    = "spotify gave no details for this track"
	reasonNoFile       = "deezer has no file for this track"
	reasonCountry      = "not available in your country"
	reasonSession      = "deezer session expired, reconnect deezer"
	reasonNetwork      = "connection lost while downloading"
	reasonRefused      = "deezer refused the download"
	reasonDiskFull     = "the disk is full"
	reasonNoWrite      = "can't write to that folder"
	reasonTagging      = "couldn't write the tags"
	reasonCancelled    = "cancelled"
	reasonGeneric      = "download failed"
)

// qualityLabel turns a wax quality ("128", "320", "flac") or a Deezer format
// name ("MP3_128", "MP3_320", "FLAC") into the words the app shows.
func qualityLabel(quality string) string {
	switch strings.ToLower(quality) {
	case "128", "mp3_128":
		return "mp3 128"
	case "320", "mp3_320":
		return "mp3 320"
	case "flac":
		return "flac"
	}
	return strings.ToLower(quality)
}

// failureReason explains why one track was not saved. err is what
// DownloadTrack returned; a nil err means Deezer offered no file at all.
func failureReason(err error, quality string) string {
	if err == nil {
		return reasonNoFile
	}
	var license *download.WrongLicense
	if errors.As(err, &license) {
		label := qualityLabel(license.Format)
		if label == "" {
			label = qualityLabel(quality)
		}
		return fmt.Sprintf("not available in %s for this account", label)
	}
	var geo *download.GeoBlocked
	if errors.As(err, &geo) {
		return reasonCountry
	}
	if errors.Is(err, context.Canceled) {
		return reasonCancelled
	}
	text := strings.ToLower(err.Error())
	has := func(needles ...string) bool {
		for _, n := range needles {
			if strings.Contains(text, n) {
				return true
			}
		}
		return false
	}
	switch {
	case has("license token", "invalid arl", "session", "401 unauthorized", "not logged"):
		return reasonSession
	case has("no space left", "disk full"):
		return reasonDiskFull
	case has("permission denied", "read-only file system", "access is denied"):
		return reasonNoWrite
	case has("metadata", "tagging", "id3", "flac header"):
		return reasonTagging
	case has("download failed: 4", "download failed: 5"):
		return reasonRefused
	case has("timeout", "timed out", "connection reset", "connection refused", "no such host",
		"eof", "broken pipe", "network is unreachable", "tls", "dial tcp"):
		return reasonNetwork
	}
	return reasonGeneric
}

// detail is the raw error for the log, lowercase and one line.
func detail(err error) string {
	if err == nil {
		return ""
	}
	text := strings.ToLower(strings.Join(strings.Fields(err.Error()), " "))
	if len(text) > 300 {
		text = text[:300] + "…"
	}
	return text
}

// missingPositions lists the positions 1..expected that no matched track
// holds. Only meaningful when positions come from the Spotify order.
func missingPositions(tracks []types.TrackType, expected int) []int {
	have := map[int]bool{}
	for _, t := range tracks {
		if t.TRACK_POSITION != nil {
			have[*t.TRACK_POSITION] = true
		}
	}
	missing := []int{}
	for i := 1; i <= expected; i++ {
		if !have[i] {
			missing = append(missing, i)
		}
	}
	return missing
}

// trackEvent reports one track that was not saved (status "failed") or was
// left alone because it is already on disk (status "skipped").
func trackEvent(status, artist, title, reason, why string) map[string]any {
	event := map[string]any{"type": "track", "status": status, "artist": artist, "title": title, "reason": reason}
	if why != "" {
		event["detail"] = why
	}
	return event
}
