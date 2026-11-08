package main

import (
	"encoding/json"
	"fmt"
	"html"
	"io"
	"net/http"
	"regexp"
	"strings"
	"time"

	"github.com/d-fi/GoFi/converter"
)

type embedEntity struct {
	Type     string `json:"type"`
	Name     string `json:"name"`
	Title    string `json:"title"`
	Subtitle string `json:"subtitle"`
	Duration int    `json:"duration"`
	Artists  []struct {
		Name string `json:"name"`
	} `json:"artists"`
	TrackList []struct {
		Title, Subtitle, URI string
		Duration             int
	} `json:"trackList"`
}

// Spotify's public embed contains metadata even when its Web API is rate-limited.
// A match is accepted only by GoFi's title, artist, version and duration checks.
func resolveEmbed(parts converter.URLParts) (converter.ParseResult, int, error) {
	kind := strings.TrimPrefix(parts.Type, "spotify-")
	if kind != "track" && kind != "album" {
		return converter.ParseResult{}, 0, fmt.Errorf("Spotify playlist is unavailable")
	}
	if !regexp.MustCompile(`^[A-Za-z0-9]+$`).MatchString(parts.ID) {
		return converter.ParseResult{}, 0, fmt.Errorf("Invalid Spotify link")
	}
	req, _ := http.NewRequest("GET", "https://open.spotify.com/embed/"+kind+"/"+parts.ID, nil)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	response, err := (&http.Client{Timeout: 20 * time.Second}).Do(req)
	if err != nil {
		return converter.ParseResult{}, 0, err
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return converter.ParseResult{}, 0, fmt.Errorf("Spotify embed returned %s", response.Status)
	}
	body, err := io.ReadAll(io.LimitReader(response.Body, 4<<20))
	if err != nil {
		return converter.ParseResult{}, 0, err
	}
	match := regexp.MustCompile(`(?s)<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>`).FindSubmatch(body)
	if len(match) != 2 {
		return converter.ParseResult{}, 0, fmt.Errorf("Spotify embed metadata missing")
	}
	var page struct {
		Props struct {
			PageProps struct {
				State struct {
					Data struct {
						Entity embedEntity `json:"entity"`
					} `json:"data"`
				} `json:"state"`
			} `json:"pageProps"`
		} `json:"props"`
	}
	if err := json.Unmarshal([]byte(html.UnescapeString(string(match[1]))), &page); err != nil {
		return converter.ParseResult{}, 0, err
	}
	entity := page.Props.PageProps.State.Data.Entity
	if entity.Type != kind {
		return converter.ParseResult{}, 0, fmt.Errorf("Spotify link returned unexpected content")
	}
	result := converter.ParseResult{Info: parts, LinkType: kind, LinkInfo: map[string]any{"ALB_TITLE": entity.Name}}
	source := []converter.SpotifyTrack{}
	if kind == "track" {
		t := converter.SpotifyTrack{Name: entity.Title, DurationMS: entity.Duration}
		for _, artist := range entity.Artists {
			t.Artists = append(t.Artists, converter.SpotifyArtist{Name: artist.Name})
		}
		source = append(source, t)
	} else {
		for _, item := range entity.TrackList {
			t := converter.SpotifyTrack{Name: item.Title, DurationMS: item.Duration, Artists: []converter.SpotifyArtist{{Name: item.Subtitle}}, Album: converter.SpotifyAlbumRef{Name: entity.Name}}
			source = append(source, t)
		}
	}
	if len(source) == 0 {
		return result, 0, fmt.Errorf("Spotify did not provide any tracks")
	}
	for i, item := range source {
		emit(map[string]any{"type": "status", "message": fmt.Sprintf("Finding track %d of %d on Deezer…", i+1, len(source))})
		if item.Name == "" || item.DurationMS <= 0 || len(item.Artists) == 0 || item.Artists[0].Name == "" {
			continue
		}
		track, err := converter.SpotifyTrackMetadataToDeezer(item)
		if err == nil {
			position := i + 1
			track.TRACK_POSITION = &position
			result.Tracks = append(result.Tracks, track)
		}
	}
	return result, len(source), nil
}
