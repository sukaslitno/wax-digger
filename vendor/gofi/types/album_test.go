package types

import (
	"encoding/json"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestAlbumContributorsUnmarshalJSON(t *testing.T) {
	tests := []struct {
		name     string
		input    string
		expected AlbumContributors
		wantErr  bool
	}{
		{
			name:     "Object with main artist",
			input:    `{"main_artist": ["Avicii"]}`,
			expected: AlbumContributors{MainArtist: []string{"Avicii"}},
		},
		{
			name:     "Empty array (album without contributor metadata)",
			input:    `[]`,
			expected: AlbumContributors{},
		},
		{
			name:     "Empty object",
			input:    `{}`,
			expected: AlbumContributors{},
		},
		{
			name:    "Invalid structure",
			input:   `["unexpected"]`,
			wantErr: true,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var ac AlbumContributors
			err := json.Unmarshal([]byte(tt.input), &ac)
			if tt.wantErr {
				assert.Error(t, err)
				return
			}
			require.NoError(t, err)
			assert.Equal(t, tt.expected, ac)
		})
	}
}

func TestAlbumTypeUnmarshalWithArrayContributors(t *testing.T) {
	// Deezer's album.getData returns ALB_CONTRIBUTORS as an empty array
	// instead of an object for albums without contributor metadata.
	data := `{"ALB_ID": "6090427", "ALB_TITLE": "Kids Original Motion Picture Soundtrack", "ALB_CONTRIBUTORS": []}`

	var album AlbumType
	err := json.Unmarshal([]byte(data), &album)
	require.NoError(t, err)
	assert.Equal(t, "6090427", album.ALB_ID)
	assert.Empty(t, album.ALB_CONTRIBUTORS.MainArtist)
}
