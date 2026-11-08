// wax-gofi adapts GoFi to Wax's local download protocol.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/d-fi/GoFi/api"
	"github.com/d-fi/GoFi/converter"
	"github.com/d-fi/GoFi/internal/dfi"
	"github.com/d-fi/GoFi/metadata"
	"github.com/d-fi/GoFi/request"
	"github.com/d-fi/GoFi/types"
)

type input struct{ URL, Quality, Output, ARL string }

func emit(event map[string]any) { b, _ := json.Marshal(event); fmt.Printf("WAX_EVENT:%s\n", b) }
func fail(message string)       { emit(map[string]any{"type": "error", "message": message}); os.Exit(1) }

func main() {
	var in input
	if err := json.NewDecoder(os.Stdin).Decode(&in); err != nil {
		fail("invalid download request")
	}
	if _, _, _, err := dfi.ParseQualityStrict(in.Quality); err != nil {
		fail("unsupported deezer quality")
	}
	parts, err := converter.GetURLParts(in.URL)
	if err != nil || (parts.Type != "spotify-track" && parts.Type != "spotify-album" && parts.Type != "spotify-playlist") {
		fail("use a spotify track, album, or playlist link")
		return
	}
	emit(map[string]any{"type": "status", "message": "connecting to deezer…"})
	if _, err = request.InitDeezerAPI(in.ARL); err != nil {
		fail("could not connect to deezer. check your arl and connection.")
	}
	user, err := api.GetUser()
	if err != nil || user.UserID == "" || user.UserID == "0" {
		fail("could not connect to deezer. check your arl and connection.")
	}
	emit(map[string]any{"type": "status", "message": "finding tracks on deezer…"})
	result, err := converter.ParseInfo(in.URL)
	expected := 0
	usedEmbed := false
	var missing []missingTrack
	if err != nil && (parts.Type == "spotify-track" || parts.Type == "spotify-album") {
		result, expected, missing, err = resolveEmbed(parts)
		usedEmbed = true
	}
	if err != nil {
		fail("could not find this spotify link on deezer: " + detail(err))
	}
	if expected == 0 {
		expected = len(result.Tracks)
	}
	switch parts.Type {
	case "spotify-track":
		expected = 1
	case "spotify-playlist":
		if p, ok := result.LinkInfo.(types.PlaylistInfo); ok {
			expected = p.NbSong
		}
	case "spotify-album":
		a, e := converter.GetSpotifyAlbum(parts.ID)
		if e == nil {
			expected = a.TotalTracks
		} else if !usedEmbed {
			result, expected, missing, err = resolveEmbed(parts)
			usedEmbed = true
			if err != nil {
				fail("could not check spotify album: " + detail(err))
			}
		}
	}
	reportMissing(parts.Type, usedEmbed, result.Tracks, missing, expected)
	if expected == 0 || len(result.Tracks) == 0 {
		fail("no matching tracks found on deezer")
	}
	saved, skipped := 0, 0
	failed := expected - len(result.Tracks)
	if failed < 0 {
		failed = 0
	}
	covers := dfi.CoverSizes{MP3_128: 500, MP3_320: 500, FLAC: 1000}
	for i, track := range result.Tracks {
		emit(map[string]any{"type": "progress", "message": fmt.Sprintf("downloading %d of %d", i+1, len(result.Tracks)), "percent": float64(i) * 100 / float64(len(result.Tracks))})
		template := filepath.Join(in.Output, "{ART_NAME} - {SNG_TITLE} ["+in.Quality+"]")
		if parts.Type != "spotify-track" {
			position := i + 1
			if track.TRACK_POSITION != nil {
				position = *track.TRACK_POSITION
			}
			template = filepath.Join(in.Output, "{TITLE}", fmt.Sprintf("%03d - ", position)+"{ART_NAME} - {SNG_TITLE} ["+in.Quality+"]")
			if parts.Type == "spotify-album" {
				template = filepath.Join(in.Output, "{ALB_TITLE}", fmt.Sprintf("%03d - ", position)+"{ART_NAME} - {SNG_TITLE} ["+in.Quality+"]")
			}
		}
		_, ext, _, _ := dfi.ParseQualityStrict(in.Quality)
		target := dfi.SaveLayout(track, result.LinkInfo, template, false, len(result.Tracks)) + ext
		if _, err := os.Stat(target); err == nil {
			// the file from an earlier run is kept: it counts as saved
			emit(trackEvent("skipped", track.ART_NAME, track.SNG_TITLE, reasonAlreadyThere, ""))
			skipped++
			saved++
			continue
		}
		path, err := dfi.DownloadTrack(context.Background(), dfi.DownloadTrackOptions{
			Track: track, Quality: in.Quality, Info: result.LinkInfo, Path: template,
			TotalTracks: len(result.Tracks), TrackNumber: false, FallbackTrack: false, FallbackQuality: false,
			CoverSizes: covers, CoverMode: metadata.CoverModeEmbed,
		})
		if err != nil || path == "" {
			emit(trackEvent("failed", track.ART_NAME, track.SNG_TITLE, failureReason(err, in.Quality), detail(err)))
			failed++
			continue
		}
		saved++
	}
	// saved includes skipped; skipped is extra detail for the log
	emit(map[string]any{"type": "result", "saved": saved, "failed": failed, "total": expected, "skipped": skipped})
	if failed > 0 || saved != expected {
		os.Exit(1)
	}
}

// reportMissing names the Spotify tracks that found no match on Deezer: by
// artist and title when the embed told us, by position on a playlist, or as
// a count when only the totals differ.
func reportMissing(kind string, usedEmbed bool, tracks []types.TrackType, missing []missingTrack, expected int) {
	if usedEmbed {
		for _, m := range missing {
			emit(trackEvent("failed", m.Artist, m.Title, m.Reason, ""))
		}
		return
	}
	count := expected - len(tracks)
	if count <= 0 {
		return
	}
	if kind == "spotify-playlist" {
		positions := missingPositions(tracks, expected)
		if len(positions) == count {
			for _, p := range positions {
				emit(trackEvent("failed", "", fmt.Sprintf("track %d", p), reasonNotFound, ""))
			}
			return
		}
	}
	event := trackEvent("failed", "", "", reasonNotFound, "")
	event["count"] = count
	emit(event)
}
