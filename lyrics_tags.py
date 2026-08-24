#!/usr/bin/env python3
"""
lyrics_tags.py — read/write the embedded-lyrics tag across the audio
formats in this library, via mutagen (the one place in this feature that
needs a real dependency: no stdlib module reads/writes ID3/Vorbis
Comment/MP4 tags, and format-specific CLI tools would mean juggling a
different tool per format instead of one consistent API).

Shared by fetch_lyrics_batch.py (checks whether a track already has
lyrics, so it isn't re-fetched) and commit_lyrics.py (writes the reviewed
lyrics in). Not used by the always-running audio_grabber_server.py service
-- this is a standalone library-maintenance tool, run by hand, so it's
exempt from this repo's "no pip deps in the core server" rule the same way
zxcvbn in vault_api.py is: it's an addition to a script that isn't part of
the always-on server, not the server itself.

Each audio format stores lyrics differently:
  - MP3 (ID3v2):      the USLT ("Unsynchronised lyrics") frame.
  - FLAC / Ogg Vorbis/Opus: a "LYRICS" Vorbis comment field.
  - M4A/MP4 (AAC):    the "\\xa9lyr" atom.
  - WAV, bare ADTS AAC: no well-supported lyrics tag -- not handled here.
"""

import mutagen
from mutagen.id3 import ID3, USLT, ID3NoHeaderError
from mutagen.flac import FLAC
from mutagen.oggvorbis import OggVorbis
from mutagen.oggopus import OggOpus
from mutagen.mp4 import MP4

UNSUPPORTED_EXTS = (".wav", ".aac")


def get_lyrics(path):
    """Returns the embedded lyrics text, or None if there are none (or the
    format isn't one we can read lyrics from)."""
    lower = path.lower()
    try:
        if lower.endswith(".mp3"):
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                return None
            for key in tags.keys():
                if key.startswith("USLT"):
                    text = tags[key].text
                    return text.strip() if text and text.strip() else None
            return None
        elif lower.endswith(".flac"):
            tags = FLAC(path)
            values = tags.get("LYRICS")
            return values[0].strip() if values and values[0].strip() else None
        elif lower.endswith(".opus"):
            tags = OggOpus(path)
            values = tags.get("LYRICS")
            return values[0].strip() if values and values[0].strip() else None
        elif lower.endswith(".ogg"):
            tags = OggVorbis(path)
            values = tags.get("LYRICS")
            return values[0].strip() if values and values[0].strip() else None
        elif lower.endswith(".m4a"):
            tags = MP4(path)
            values = tags.get("\xa9lyr")
            return values[0].strip() if values and values[0].strip() else None
    except mutagen.MutagenError:
        return None
    return None


def set_lyrics(path, text):
    """Embeds lyrics text into the file. Returns (ok, error_or_None)."""
    lower = path.lower()
    if lower.endswith(UNSUPPORTED_EXTS):
        return False, f"format has no well-supported lyrics tag: {path}"
    try:
        if lower.endswith(".mp3"):
            try:
                tags = ID3(path)
            except ID3NoHeaderError:
                tags = ID3()
            # Remove any existing USLT frames first -- otherwise re-running
            # this on a file that already has one (e.g. a bad old one)
            # would add a second frame instead of replacing it.
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="", text=text))
            tags.save(path)
        elif lower.endswith(".flac"):
            tags = FLAC(path)
            tags["LYRICS"] = text
            tags.save()
        elif lower.endswith(".opus"):
            tags = OggOpus(path)
            tags["LYRICS"] = text
            tags.save()
        elif lower.endswith(".ogg"):
            tags = OggVorbis(path)
            tags["LYRICS"] = text
            tags.save()
        elif lower.endswith(".m4a"):
            tags = MP4(path)
            tags["\xa9lyr"] = text
            tags.save()
        else:
            return False, f"unrecognized audio format: {path}"
        return True, None
    except mutagen.MutagenError as e:
        return False, str(e)
