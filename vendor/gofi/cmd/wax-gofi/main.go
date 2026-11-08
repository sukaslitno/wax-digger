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
		fail("Invalid download request")
	}
	if _, _, _, err := dfi.ParseQualityStrict(in.Quality); err != nil {
		fail("Unsupported Deezer quality")
	}
	parts, err := converter.GetURLParts(in.URL)
	if err != nil || (parts.Type != "spotify-track" && parts.Type != "spotify-album" && parts.Type != "spotify-playlist") {
		fail("Use a Spotify track, album, or playlist link")
		return
	}
	emit(map[string]any{"type": "status", "message": "Connecting to Deezer…"})
	if _, err = request.InitDeezerAPI(in.ARL); err != nil {
		fail("Could not connect to Deezer. Check your ARL and connection.")
	}
	user, err := api.GetUser()
	if err != nil || user.UserID == "" || user.UserID == "0" {
		fail("Could not connect to Deezer. Check your ARL and connection.")
	}
	emit(map[string]any{"type": "status", "message": "Finding tracks on Deezer…"})
	result, err := converter.ParseInfo(in.URL)
	expected := 0
	usedEmbed := false
	if err != nil && (parts.Type == "spotify-track" || parts.Type == "spotify-album") {
		result, expected, err = resolveEmbed(parts)
		usedEmbed = true
	}
	if err != nil {
		fail("Could not find this Spotify link on Deezer: " + err.Error())
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
			result, expected, err = resolveEmbed(parts)
			if err != nil {
				fail("Could not check Spotify album: " + err.Error())
			}
		}
	}
	if expected == 0 || len(result.Tracks) == 0 {
		fail("No matching tracks found on Deezer")
	}
	saved := 0
	failed := expected - len(result.Tracks)
	if failed < 0 {
		failed = 0
	}
	covers := dfi.CoverSizes{MP3_128: 500, MP3_320: 500, FLAC: 1000}
	for i, track := range result.Tracks {
		emit(map[string]any{"type": "progress", "message": fmt.Sprintf("Downloading %d of %d", i+1, len(result.Tracks)), "percent": float64(i) * 100 / float64(len(result.Tracks))})
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
			failed++
			continue
		}
		path, err := dfi.DownloadTrack(context.Background(), dfi.DownloadTrackOptions{
			Track: track, Quality: in.Quality, Info: result.LinkInfo, Path: template,
			TotalTracks: len(result.Tracks), TrackNumber: false, FallbackTrack: false, FallbackQuality: false,
			CoverSizes: covers, CoverMode: metadata.CoverModeEmbed,
		})
		if err != nil || path == "" {
			failed++
			continue
		}
		saved++
	}
	emit(map[string]any{"type": "result", "saved": saved, "failed": failed, "total": expected})
	if failed > 0 || saved != expected {
		os.Exit(1)
	}
}
