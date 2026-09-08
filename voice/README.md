# Zeta's voice

Drop a reference recording here to change how Zeta sounds. Chatterbox clones a voice
from the clip at generation time, so there is nothing to train.

A good reference clip is:

- one speaker, no music, no echo, little background noise
- 7 to 15 seconds of normal conversational speech
- WAV, mono, 16 kHz or higher

Then point Zeta at it in `.env`:

```
CHATTERBOX_VOICE=voice/zeta_female.wav
```

Restart the voice server (close its window, or restart Zeta) and the new voice is live.

With no clip set, Chatterbox uses its own built-in voice.

Only use a voice you have the right to use: your own, a synthetic one, or one whose owner
has agreed. Cloning someone's voice without their permission is not okay, and in many
places it is also illegal.
