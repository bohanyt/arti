# ARTI

ARTI is an Indonesian-speaking AI VTuber who plays Stardew Valley by herself, live, as a streamer. She joins a
co-op farm as a farmhand, decides what to do with her day, does it with her own hands in the real game, and talks
with chat while she plays. The goal is that she plays the game through to its end by her own choices, understands
it, enjoys it, and is fun to watch on the way.

YouTube: https://www.youtube.com/@bohanyt

## What she does

- **Plays.** She moves, uses tools, interacts with the world, works any menu or dialog, and goes to bed, through
  the same kinds of input a player has. Nothing is scripted per task: she is shown what she can do right now, and
  she chooses.
- **Learns the game as facts, never as orders.** Seasons, crops, money, shops and the Community Center reach her
  as things that are true. What to do about them stays her decision.
- **Remembers.** She keeps what she did yesterday, what changed in the world and what she is working toward,
  across days and across sessions.
- **Talks.** The ARTI who answers chat is the same ARTI who chooses and plays. She reacts to her own journey
  instead of narrating it.

## How it is built

- **Hands: a C# SMAPI mod** (`stardew-mod/`). It runs inside her copy of the game, reports what is around her,
  carries out her inputs, and reads the result back from game state instead of assuming a command worked.
- **Mind: a Python runtime.** Each moment it builds a "page": what she sees, what she remembers, and the choices
  open to her. A language model picks one, the runtime runs it through the mod, then checks the effect in the world.
- **Voice bridge.** Speech to text for the microphone, chat intake, and text to speech for her voice, kept apart
  from the game loop so that talking stays fast while she plays.
- **VTube Studio.** Her avatar: expressions, idle motion and reactions, with a subtitle overlay on stream.
- **Two games, one farm.** A host game owns the farm. ARTI runs a separate SMAPI-enabled game and joins it as a
  farmhand over ordinary multiplayer.

An older Minecraft integration is also in the tree. It is not the current focus.

`docs/WIRING.md` shows how the parts connect.

## Status

Experimental. This is a working lab and not a product: one person's project, run by its author on the author's
own machines, and pushed forward with AI coding agents. Nothing is packaged to install and run. It is published
so the work can be seen, read and judged.

She plays real sessions, but she is slow for a streamer and she still gets stuck. A change that passed its tests
is not counted as proven in the game.

## What is not in this repository

- **Recorded sessions.** Recordings, transcripts and logs of real streams carry viewers' names and voices. They
  are not the author's to publish.
- **Game assets and game text.** Saves, screenshots, image crops and test fixtures that contain the game's own
  text belong to the game's owner. You need your own copy of Stardew Valley.
- **Private memory.** Her persona file, her memory, and the viewer and mood files are personal to the channel and
  to the people in it. The `*.example.*` files show the shape of what is missing.
- **Keys and tokens.** Bring your own.
- **Game-derived tables and ports.** Larger tables read from the game's content files (crop data, festival
  dates, bundle rows) and offline ports of game routines are left out, so some of the public code is incomplete:
  the crop table module ships as a stub without data. Short game identifiers (item ids, place and character
  names, menu class names), a few re-implemented game rules and some short on-screen strings the code matches
  are still in the code.
- **The C# test projects.** They need the game's assemblies and inputs that are not here; the public C# is the
  mod itself.
- **The working notes.** Design records, run reports, one-off analysis scripts and the coordinator's notes quote
  sessions and the game, so this first release leaves them out.

`docs/WHAT_IS_NOT_IN_THIS_REPO.md` has the longer list. Because of this, code and tests sometimes name a file
that is not here. Tests that replayed recorded sessions or game captures are left out. The Python tests that
ship use inputs made for them; some began as the shape of a real game state or a real session, with the game's
text and recorded speech replaced by invented text (`docs/TESTING.md`).

## Setup

Set-up is by hand and is not automated. `docs/SETUP.md` is a short map of it. In short you need:

- Stardew Valley with SMAPI, in two instances (the host and her farmhand);
- the .NET SDK, to build the mod;
- Python environments for the runtime, the tests and the voice bridge (`requirements*.txt`);
- VTube Studio, for the path with voice and avatar;
- a language-model provider, with your own account or key.

Copy the `*.example.*` files to their real names and fill in your own values. That gives a plain ARTI: the
example persona and an empty memory. No save ships: start a new co-op farm in your own game for her to join.

## Licence

MIT, see `LICENSE`. The licence covers the original ARTI material in this repository. Stardew Valley, SMAPI,
VTube Studio, Minecraft, and the libraries and models this code talks to belong to their own owners and are not
included. This project is not affiliated with any of them. Third-party credits are in `NOTICE`.
