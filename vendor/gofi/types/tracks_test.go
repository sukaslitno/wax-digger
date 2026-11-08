package types

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestSongContributorsUnmarshalJSON(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		expected SongContributors
		wantErr  bool
	}{
		{
			name:  "Object with contributors",
			input: `{"main_artist": ["Avicii"], "composer": ["Ash Pournouri"]}`,
			expected: SongContributors{
				MainArtist: []string{"Avicii"},
				Composer:   []string{"Ash Pournouri"},
			},
		},
		{
			name:     "Empty array (song without contributor metadata)",
			input:    `[]`,
			expected: SongContributors{},
		},
		{
			name:     "Empty object",
			input:    `{}`,
			expected: SongContributors{},
		},
		{
			name:    "Invalid structure",
			input:   `["unexpected"]`,
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var sc SongContributors
			err := json.Unmarshal([]byte(tt.input), &sc)
			if tt.wantErr {
				assert.Error(t, err)
				return
			}
			require.NoError(t, err)
			assert.Equal(t, tt.expected, sc)
		})
	}
}

func TestSongTypeUnmarshalWithArrayContributors(t *testing.T) {
	// Deezer can return SNG_CONTRIBUTORS as an empty array instead of an
	// object for songs without contributor metadata.
	data := `{"SNG_ID": "6090427", "SNG_TITLE": "Kids", "SNG_CONTRIBUTORS": []}`

	var song SongType
	err := json.Unmarshal([]byte(data), &song)
	require.NoError(t, err)
	assert.Equal(t, "Kids", song.SNG_TITLE)
	assert.NotNil(t, song.SNG_CONTRIBUTORS)
	assert.Empty(t, song.SNG_CONTRIBUTORS.MainArtist)
}
