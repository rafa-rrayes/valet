# Valet for iOS and macOS

Native SwiftUI apps for Valet's control plane: the agents, their sessions (live), the inbox, the agent's computer,
and settings. They use
the same REST API and client WebSocket as the web UI ([docs/API.md](../docs/API.md)). iOS 17 and macOS 14 or later,
Swift 6 with strict concurrency.

## Layout

| Path | What |
|---|---|
| `project.yml` | The XcodeGen spec. `Valet.xcodeproj` is generated from it and committed: run `xcodegen` here after adding or removing files. |
| `ValetKit/` | A Swift package with all the shared code (below), and its unit tests. |
| `ValetKit/Sources/ValetKit/API/` | `JSONValue` and the lenient decoding helpers, the models (`Models.swift`), socket frames, `APIClient` (every route the apps use), `ClientSocket` (`/ws/client`). |
| `ValetKit/Sources/ValetKit/Conversation/` | `EventFolder`: a session's events folded into its conversation (messages, thoughts, tool cards, permissions, plans, turn ends). A pure value type, the Swift twin of `app.js`'s Renderer. |
| `ValetKit/Sources/ValetKit/Model/` | `AppModel` (sign-in, lock, the live state from the socket, push registration, frame subscriptions for the screens), `SessionModel` (one open session: history, then live events), `SharedList` (an agent's shared space, live), `AgentSettings` (an agent's settings page: the saved definition and the draft the form edits). |
| `ValetKit/Sources/ValetKit/Computer/` | The agent's computer: `TerminalSession` (one shell: the `/ws/terminal/{agent}` socket, its size, output in order, its end), `TerminalCanvas` (SwiftTerm's view on iOS and macOS: theme, size reports, links), `FilesModel` (one Files screen: a folder or a file, uploads with Replace / Keep both, new folders, volumes). |
| `ValetKit/Sources/ValetKit/Live/` | The browser's live view: `LiveSession` (the `/ws/browser/{profile}` socket: JPEG frames off the main thread, tabs, busy, input) and its canvas (a `UIView` with touch, pinch and momentum on iOS; an `NSView` with the mouse, wheel and keyboard on macOS). |
| `ValetKit/Sources/ValetKit/Views/` | The screens: sign-in and lock, Agents, an agent, a session (and its attachments), the terminal, Files, Inbox (browser asks too), the live view, Shared, an agent's memory, its settings, its triggers and runs, browser profiles and the vault, continuing and handing off, Settings, the root (tabs on iOS, a sidebar and a menu bar extra on macOS). |
| `ValetKit/Sources/ValetKit/Support/` | Keychain, Face ID / Touch ID, push, platform helpers, the debug launch hooks. |
| `ValetKit/Sources/ValetPushCategories/` | The notification categories and which one fits a push; shared by the apps and the iOS notification extension. |
| `iOS/`, `macOS/` | Each app's entry point and app delegate (push registration, notification actions, lock on background or screen lock). |
| `Notify/` | The iOS Notification Service Extension: narrows a push's buttons to its kind. |
| `UITests/` | The end-to-end UI test against a real control plane (below). |
| `Shared/` | The asset catalog (icon, accent color). |

Bundle ID `br.com.rrayes.valet` for both apps (one APNs topic), team `N7Z47ZX3FQ`, display name "Valet" (the
notification extension is `.notify`, the UI tests `.uitests`).

## Building

Xcode 26 and [XcodeGen](https://github.com/yonaskolb/XcodeGen) (`brew install xcodegen`).

```sh
cd apps
xcodegen                                         # after adding or removing files
xcodebuild -project Valet.xcodeproj -scheme Valet-iOS -destination 'generic/platform=iOS Simulator' build
xcodebuild -project Valet.xcodeproj -scheme Valet-macOS -destination 'platform=macOS' build
(cd ValetKit && swift test)                      # the unit tests
```

`CODE_SIGNING_ALLOWED=NO` builds without signing (a check that it compiles), but an unsigned app can't use the
Keychain: it says so after signing in, and asks for the password at every launch. For the simulator, sign to run
locally instead (`CODE_SIGN_IDENTITY=-`, no Apple account needed). On the Mac the app uses the data-protection
keychain only (never the login keychain, whose items prompt for the password whenever a rebuilt app reads them),
which needs a build signed with the team; an ad-hoc one runs (`CODE_SIGN_IDENTITY=- CODE_SIGN_ENTITLEMENTS=`) but
doesn't remember the sign-in. To run on a device, open `Valet.xcodeproj`, pick
the Valet-iOS scheme and your iPhone; signing is automatic with the team above. Real pushes need a build signed
with the team (for the `aps-environment` entitlement) and the server's APNs key; `xcrun simctl push` shows a
notification and its buttons in the simulator without either.

The terminal is [SwiftTerm](https://github.com/migueldeicaza/SwiftTerm) 1.18.0 (MIT, Miguel de Icaza; it carries
the xterm.js authors' notice), pinned exactly in `ValetKit/Package.swift`. Later versions run a build-tool plugin
that Xcode makes you trust by hand (and `xcodebuild` needs `-skipPackagePluginValidation`); 1.18.0 has none.

## Pointing the app at a control plane

On first launch the app asks for the server address and the owner's password. `127.0.0.1:8790`, `localhost`,
`*.local` and IP addresses default to `http://`; anything else to `https://` (type the scheme to override). The
address can include a path when Valet sits behind a reverse proxy under one (`https://example.com/valet`).

Signing in is `POST /api/login {"password", "client": "native", "device"}`: the server answers with a bearer
token (`vown_…`, 30 days) that the app keeps in the Keychain (this device only, after first unlock) and sends as
`Authorization: Bearer` on every request and on the socket. Signing out (Settings) revokes it, and unregisters
the device for push. A token the server refuses (expired, revoked) signs the app out.

The app stays live over `/ws/client`: a `hello` with everything, then a frame for each change. It reconnects on
its own (1 s, doubling up to 30 s), and refetches an open session's events after the last one it applied, so
nothing is missed or applied twice. On iOS it reconnects when it comes back to the foreground.

An iPhone reaches a control plane on your Mac over the LAN (`http://mac.local:8790`, allowed by the app's local
networking exception) or through whatever puts Valet on the internet (Tailscale, a tunnel, a reverse proxy with
TLS). The control plane must listen on an address the phone can reach (`VALET_HOST`).

## What's in it

- **Agents**: emoji, display name, status (idle, working, waiting on you, stopped, failed, runner offline), what
  it's doing while it works, queued prompts and its next trigger (M4), "restart to apply", missing secrets, and
  "N new in Shared" when it added or changed something in its Shared since you looked (the Agents tab's badge
  counts them all; on the Mac, a count by the agent in the sidebar). Start, Stop and Restart from a swipe, a
  context menu or the agent's page.
- **Delete agent…** (the web UI's): at the bottom of the agent's settings, in its row's context menu and in the Mac
  sidebar's. The confirmation says what goes (its settings, personality, skills and memory leave the definitions
  repo, whose history keeps them; its sessions, inbox items, Shared notes and files, queued messages, triggers and
  wake-ups go for good), that a host agent's working folder stays, and, for a container agent that was placed on a
  runner, offers to delete its volumes too. A refusal (its runner not connected, say) shows under the button.
  Once it's deleted, here or on another device (`agent_deleted`), its screens close (the stack they're in goes
  back to its first screen; the Mac's sidebar falls back to All agents) and it leaves every list.
- **New agent** (the web UI's New agent card): + on the Agents screen (and on an empty list), the + by the Mac
  sidebar's Agents, and File > New Agent… (⌘N, greyed out while the form is open). A name (the ID follows it, lowercase letters, digits and dashes,
  31 at most; Change to type one), an emoji (empty: the template's or the copied agent's), what it starts from (a
  template, with its personality as a preview, or a copy of an agent: its settings, personality and skills, with a
  computer and sessions of its own), and its brain (each harness with Ready, No key on the runner and the key it
  needs, or unknown while no runner is connected; the model, or Other…). The form checks the name, the ID's form,
  `owner` and an ID that's taken before sending; a refused create (`POST /api/agents`) shows each error under its
  field. Create & start creates it, starts it and opens its page, which says so when it didn't start (no runner
  connected, a missing secret). **Copy to a new agent…** on an agent's page, its row's context menu and the Mac
  sidebar's opens the same form copying it (an agent's ID never changes; its name and emoji are in its settings).
- **An agent**: where it stands and why (its error), Start / Stop / Restart, its sessions newest first, New
  session; its main session, its settings, its memory, the live view of each browser profile it uses, continuing a
  Claude Code session and handing off a conversation. What wakes it (M4): its schedules and webhooks (when each runs
  next, where it goes, how the last run went), the wake-ups it set itself (Cancel), and its recent runs, each opening
  the session it went to; in the list, its next trigger and the prompts waiting. A start refused for missing secrets
  asks for them (write-only fields: a secret goes to `PUT /api/secrets/{name}`, a vault entry's password, named
  "github/alice's password", to `PUT /api/vault/{entry}`) and tries again.
- **A session**: the conversation, live: your messages, the agent's replies as Markdown, thoughts folded, tool
  calls as cards (diffs, terminals, input and output), plans, turn ends that say why (cancelled, refused, an
  error), with Retry on one that failed or was interrupted (gone once it's sent again). A permission request sits on
  its tool card with the item's options. Where a prompt came from when you didn't type it ("Schedule · Every day at
  08:00", "Webhook deploy", "Sent again"). The composer is multi-line (⌘Return sends on the Mac), with Cancel while a
  turn runs; a prompt that arrives while the agent works waits in the session's queue (M4), shown until its turn
  starts, and can be removed. The agent's main session says so under its title. It follows the end, with "Latest"
  when you've scrolled up. **Attachments**: the paperclip (Photos, Files), drops (files, or a picture dragged from
  another app), and ⌘V on the Mac (a picture, or files copied in the Finder; text pastes as text); each uploads at once and shows as a chip above the text (thumbnail or icon, name,
  size, ×), at most 10. Send waits for them and carries them with the text; refused, the text and the chips stay.
  In the conversation your pictures show (tap to enlarge) and your files are rows that open them in Files. A
  hand-off folds into "Handed off: <where from>" with the condensed conversation inside, and its note shows as
  yours. The agent's own messages and questions (valet.owner) show as they do in the Inbox, answered in place.
- **The agent's computer** (M4), from its page: **Terminal**, its shells as tabs (at most 8), each sized to its view
  (rotate, split, the keyboard), light and dark, 5000 lines of scrollback (the web UI's), with a row of keys above
  the iOS keyboard (Esc, Ctrl, Tab, arrows); the shells end when you leave the screen. **Files**, one screen per folder with breadcrumbs (and its
  places when it has several): previews of text and pictures, Quick Look, Share (iOS) or Save… and Open (macOS);
  upload from Files and Photos (or drop, on the Mac), with Replace / Keep both when the name is taken; New folder. A
  stopped container agent's volumes are on its first screen, with Delete volumes behind a confirmation naming
  each one.
- **Inbox**: everything waiting on you, newest first, with its details and every option it offers (a harness's
  permission shows Allow once and Reject when it offers those). Only the first allowing option is prominent; a
  rejecting one is red. An agent's message or question reads "<Agent> asks" (or "says") with its text as Markdown;
  a question takes your own words, a choice, or both (a choice carries the words typed; Dismiss never does).
  Settled, an item says how: "You answered: <choice> · <words>", Dismissed, Expired or Closed. The tab and the app
  icon show the count.
- **Browser asks**: in the Inbox and on the session, what the agent wants to do (site, kind, action, the element or
  text), the screenshot taken just before (tap to enlarge), and every option in order; Look at the tab first. A
  take-over (`request_human`) opens the live view, where Done and I can't answer it.
- **The live view**: the profile's Chrome, streamed (JPEG frames), on iOS and macOS. It fills its space: the view
  says its size and its screen's scale (`viewport`, at most 2x) and the page is laid out at them while you watch,
  upright or on its side on a phone (where the bars shrink to a row each) and in any Mac window, sharp on a Retina
  screen; a server without the message shows the page letterboxed.
  Tap to click, drag, scroll
  (two fingers, or the wheel), pinch to zoom the picture; typing through a field (its text goes to the focused
  element on the page) and a row of keys; the address bar, Back, Reload, the tabs and New tab. While an agent's
  action holds the browser it says so, and input waits for it.
- **Shared**: each agent's own shared space with you (no other agent sees it), on the agent's page. There's no
  Shared tab: what's new in an agent's space shows on its row ("N new in Shared"), as a badge on the Agents tab
  (iPhone) and on its sidebar row (Mac). While there are any, the items with no agent from when Shared was one list
  show under the agents ("Shared items with no agent"), each to move to its agent or delete. An agent's space: newest
  first, live, a dot on each unseen item; search, filter by who added it and kind, older pages. Notes are read as Markdown, written and edited; files show a
  preview (pictures, text), open in Quick Look, and go out through the share sheet (iOS) or Save and Open (macOS).
  Upload from Files, Photos, by dropping files or pictures onto the list, or with ⌘V on the Mac, each showing while
  it uploads and saying which failed and why.
- **Memory**: an agent's memory files with their sizes against the limits and whether memory is on; read one,
  write or create one, delete it, its history, and restore an earlier version.
- **An agent's settings**: the web UI's form (name and emoji, personality with templates, harness and model,
  computer level, network, shared folders and image, permission level, what it knows (About you, a standing
  authority preset with the one suggested for its level, its environment, and a preview of what it's told), its
  tool servers with each tool's rule and the hour's grants, its skills (all the shared ones or some, and its own),
  secrets, triggers, browser profiles and site rules, memory), saved whole through
  `PUT /api/agents/{name}/definition`; a refused save shows each error under its field. Triggers: a schedule shows
  what it means as it's typed (and presets), a webhook its address and key (Show, Copy, New key); each has its
  prompt and goes to the main session or a new one. Save & restart, Discard,
  and a warning before leaving with unsaved changes. agent.yaml and persona.md as text for the rest, and their
  history to restore.
- **Settings**: the server, the lock; what agents share: **About you & authority** (owner.md, the authority
  presets: edit, new, delete when unused), **Skills** (the shared library: description, instructions, files; new,
  delete) and **Secrets & tools** (secrets: new, change, delete; the tool servers with their secrets and
  mcp.yaml as text; recent tool calls, older pages); the **browser**: its profiles (running, tabs, whether an agent
  is acting, looked at again every 15 s; site rules, Chrome, Open a tab, the live view, delete), the vault's logins
  by profile ("no password" when there's none yet; passwords and 2FA secrets are write-only), what's allowed for an
  hour (Revoke), and the activity log (everyone or one agent, every profile or one, how each action ended, the page
  then; again on a reconnect, Refresh, or a browser ask settled); notifications (push, the webhook with a test that saves it first and says what happened to each),
  the devices registered for push and when each last got one (remove one), runners, sign out.
- **Continue and hand off**: a host-level Claude Code agent continues one of Claude Code's sessions in its working
  folder with its whole history; any agent can start a session from a condensed earlier conversation (a Claude Code
  transcript on a runner's machine, or a Valet session of any agent), with a note.
- **Lock**: Face ID / Touch ID / passcode at launch and after 60 s in the background (iOS), or when the screen
  locks or the Mac sleeps (macOS). On by default when the device has one of them; Settings turns it off. The
  app switcher shows a cover, not the conversation. A failed unlock says why, and Sign out is the way out.
- **iPad**: the tabs become a sidebar on iOS 18 (the tab bar's button), and lists keep a reading width (760 pt)
  on a wide screen, centred in what the sidebar leaves (the Mac's grouped forms too).
- **macOS**: a sidebar (All agents, Inbox, each agent with what's new in its Shared as a badge, Settings) with the
  agent's page and sessions in the detail, and a menu bar extra with the pending count and the asks, answerable from
  there. An agent's row has Copy… and Delete agent… in its context menu. The sidebar follows you (an agent opened
  from All agents is selected there), keeps its selection while the agents reload after a reconnect (an agent that's
  deleted or gone for good falls back to All agents), and is restored at the next launch.

Known gaps: drags onto the composer, Files and Shared and Shared's ⌘V are tested on their item providers and
pasteboards, not driven by hand (the composer's ⌘V of a file and of a picture was); pasting text into the composer
is likewise covered by a unit test only. Files dragged from the iPad's Files app (rather than the Mac's Finder, or a
picture) may not be taken: a drop takes file URLs and images, and that wasn't tried on an iPad. A headless Chrome
(`VALET_HEADLESS=1`) streams frames at CSS pixels whatever the scale asked for, so there the live view is softer on a
Retina screen; a headed one (production, on Xvfb) sends them at the scale. Saving an agent's triggers leaves "Saved changes apply after a restart"
up (the server's `restart_needed`), though the scheduler reads the saved agent.yaml. SwiftTerm's caret blinks
forever, which makes XCUITest wait for "idle" after a hidden shell is shown again.

The accent is violet (#7C5CE0 in light, #9B7BF7 in dark: #A78BFA was too light under white button text). Larger
type sizes stack the agents' rows and an agent's header instead of cutting them off.

Unknown fields, statuses, event kinds, inbox kinds and frame types don't break anything: they're kept, shown as
well as can be, and ignored where nothing fits.

## Known gaps

Against the web UI and the server:

- Renaming an agent isn't a thing: the ID is fixed (copy the agent for another one), and the name and emoji are in
  its settings.
- The lock's screen wasn't seen in the simulators (they have no Face ID enrolled); its error and Sign out are
  checked in code only.
- On the Mac the Browser screen (a plain list) still fills the window; the grouped forms (Settings' screens, an
  agent's settings) take the reading width.

## Push

After sign-in the app asks to notify, registers with APNs and sends its token to `POST /api/devices` (a server
without that route is fine: Settings says push isn't available). The control plane sends a push for every new
inbox item once it has an APNs key: see `VALET_APNS_KEY_FILE`, `VALET_APNS_KEY_ID`, `VALET_APNS_TEAM_ID`,
`VALET_APNS_TOPIC` (this bundle ID) and `VALET_APNS_SANDBOX=1` for development builds, in the main README.

A push has category `VALET_INBOX` and `item_id`, `agent` and `kind` beside `aps`. Its buttons are Allow once,
Allow for an hour and Reject; each is mapped onto what the item really offers when it's pressed (an hour that
isn't offered becomes once, never more) and answers from the background once the device is unlocked. A tap opens
the item. A push for something an agent added to its Shared (`kind` `shared`, with `shared_id`) has no buttons; a
tap opens that item under the agent's Shared (an item with no agent: under Shared items with no agent). On iOS the notification extension narrows the buttons to the kind (a harness's permission: Allow once
and Reject; a question: Reply and Dismiss; a message: Dismiss), but only for pushes the server sends with
`"mutable-content": 1` in `aps`; without it every push shows the full set.

## Debug launch hooks

Debug builds read these environment variables (for `xcrun simctl launch`, prefix each with `SIMCTL_CHILD_`).
They're compiled out of release builds.

| Variable | Does |
|---|---|
| `VALET_DEBUG_SERVER`, `VALET_DEBUG_PASSWORD` | sign in to that server with that password |
| `VALET_DEBUG_RESET` | sign out and forget the server first (a fresh install) |
| `VALET_DEBUG_NOLOCK` | skip the lock |
| `VALET_DEBUG_NOPUSH` | don't ask to notify |
| `VALET_DEBUG_TAB` | `agents`, `inbox` or `settings` |
| `VALET_DEBUG_AGENT=name` | open an agent's page |
| `VALET_DEBUG_OPEN` | with `VALET_DEBUG_AGENT`: `terminal`, `files`, `files:/a/path`, `settings`, or `shared` |
| `VALET_DEBUG_SESSION=agent/id` | open a session |
| `VALET_DEBUG_ITEM=id` | open an inbox item |
| `VALET_DEBUG_NEW_AGENT` | the New agent form (`1`), or a copy of an agent (`copy:name`) |
| `VALET_DEBUG_NEW_AGENT_NAME` | with it: the name typed in; `VALET_DEBUG_NEW_AGENT_CREATE` then presses Create & start (the first form only, not a Copy… opened later) |
| `VALET_DEBUG_APPEARANCE` | Mac: `light` or `dark`, whatever the system's |
| `VALET_DEBUG_LIVE=profile` | open a browser profile's live view |
| `VALET_DEBUG_LANDSCAPE` | turn the iPhone (the simulator) on its side |
| `VALET_DEBUG_SCREEN` | a screen over the first: `browser`, `vault`, `secrets`, `directives`, `skills`, `profile:<name>`, `settings:<agent>` |

They're read from the environment `simctl` runs in, not from its arguments:

```sh
SIMCTL_CHILD_VALET_DEBUG_SERVER=http://127.0.0.1:8790 SIMCTL_CHILD_VALET_DEBUG_PASSWORD=... \
SIMCTL_CHILD_VALET_DEBUG_NOLOCK=1 SIMCTL_CHILD_VALET_DEBUG_NOPUSH=1 \
  xcrun simctl launch booted br.com.rrayes.valet
```

## Tests

`swift test` in `ValetKit/` runs the unit tests: decoding every API shape (with recorded fixtures from a real
control plane, and the shapes M3 and M4 add), unknown fields and kinds, the event folder, the API client against
a stub `URLProtocol`, the socket's frames, reconnects and backoff, the models, the terminal's session against a
fake connection, the Files screen's model (uploads and name clashes, folders, previews, volumes), attachments
(the upload, the prompt's body, drafts), drops and pastes (files and pictures, Shared's uploads, ⌘V only in its own
window), the live view's size and geometry, the terminal's scrollback, the Mac sidebar's saved selection, new
agents (the ID from the name, the form's checks, the request's body from a template or a copy, a refusal's errors
under their fields, Create & start), deleting an agent (the request, its screens closing at once or at the next
`hello` after a reconnect, a refusal keeping it, its Shared counts fetched again, a Shared push opening its item), owner asks and hand-offs in the conversation, and what agents share (skills'
edits, authority presets in an agent's directives, the routes of secrets, tool calls and the browser's log). The
Xcode schemes run them too.

The UI tests drive the iOS app against a running control plane with host-level agents on the fake harness
(`tests/fake_agent.py`). `testPhaseOneFlow`: sign in, open the agent, start it, a new session, `hello` streams
back, `permission` answered on its tool card, another answered in the Inbox, the session reopened with both settled,
a `slow` turn cancelled. `testPhaseTwoFlow` (an agent `scout` at `ask` with valet.browser, valet.shared, browser
profile `web` and memory on, and `TEST_RUNNER_VALET_E2E_PAGE`, an http page with a Save button and a text box that
takes focus on a click elsewhere): the agent, driven through the API, opens the page and clicks Save; the ask, with
its screenshot, is on the session and answered in the Inbox; a take-over opens the live view, which shows the page,
and a tap, typing and Backspace reach it (the agent's snapshot afterwards shows the text); Done hands it back; the
agent shares a file (allowed in the Inbox); the agent's Shared (from its page) lists it with an uploaded picture, a new note is written and each
opens with its preview; a memory file is created; the permission level is changed, saved and changed back; the
browser profiles. `testAlwaysOnFlow` (the same agent, with valet.owner): a failed turn's Retry (only the newest
keeps one), a queued prompt removed, a schedule (its preview) and a webhook added and saved in the form, the webhook
called with its key, a wake-up the agent sets and that's cancelled on its page, the recent runs, and the main
session they went to. `testComputerFlow` (a host-level agent, `TEST_RUNNER_VALET_E2E_COMPUTER_AGENT`, default
`helper`): the terminal (`echo hi` and its output, the shell's size after a rotation, a second tab, both shells
closed on leaving), Files (the text file the shell wrote, previewed; a new folder; `report.txt` uploaded from the
document picker, then again with Keep both; a photo), and attachments (a photo and `report.txt` sent with a prompt,
shown in the conversation, the file opened from it). It needs a `report.txt` in the simulator's On My iPhone (copy
one into the `File Provider Storage` folder of the Files app's group container, under the device's
`data/Containers/Shared/AppGroup`); `TEST_RUNNER_VALET_E2E_COMPUTER_PARTS` (`terminal,files,attachments`) runs only
some. Leaving a shell that was hidden under another tab makes XCUITest wait a minute for "idle" (SwiftTerm's caret
blinks with an animation that never ends), so its run is slow. `testNewAgentFlow` (the host-level agent,
`TEST_RUNNER_VALET_E2E_AGENT`, default `helper`, titled `TEST_RUNNER_VALET_E2E_AGENT_TITLE`): the New agent form's
own checks (no name, the agent's own ID taken), an emoji the server refuses shown under its field, a copy of the
agent ("Helper 37", a new number each run) created and started on its page, and Copy… there.
`testSharedAndDeleteFlow` (the same agent, with valet.shared): the agent leaves a note in its Shared through the
gateway and its row says "1 new in Shared" (there's no Shared tab); a copy ("Notes 37") made in the form starts
with an empty Shared, gets a note and a photo (its preview opens), and the first agent's Shared has its own note,
not the copy's; the copy is deleted from its settings (what goes, no volumes switch for a host agent), its screens
close and the server answers 404; a spare agent made through the API is deleted from its row's context menu. They're skipped without a server:

```sh
TEST_RUNNER_VALET_E2E_SERVER=http://127.0.0.1:PORT TEST_RUNNER_VALET_E2E_PASSWORD=... \
TEST_RUNNER_VALET_E2E_AGENT_TITLE=Helper TEST_RUNNER_VALET_E2E_SHOTS=/tmp/shots \
xcodebuild test -project Valet.xcodeproj -scheme Valet-iOS -destination 'platform=iOS Simulator,name=iPhone 17' \
  -only-testing:Valet-iOSUITests CODE_SIGN_IDENTITY=-
```

For `testPhaseTwoFlow` add `TEST_RUNNER_VALET_E2E_PAGE=http://127.0.0.1:PAGEPORT/` (the profile's rules allow only
http and https, so serve the page, e.g. `uv run python -m http.server`).

`TEST_RUNNER_VALET_E2E_SHOTS` (a directory) saves a screenshot of each step.
