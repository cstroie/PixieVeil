# Scanners & protocols

PixieVeil has to know, for every study, **which scanner** it came from and **which
protocol** was run. It needs this to:

- send the study to RHYTHM. Each upload names one RHYTHM protocol by its UUID, and
  RHYTHM derives region, indication, contrast, protocol type, examination group and
  scanner from it.
- pre-fill the study's **region / clinical indication / IV contrast** on `/studies`,
  so the operator only adds image quality, weight and age.

All of this is set up on the dashboard's **Protocols** page (`/protocols`). Nobody
needs to edit files on the server.

---

## The page at a glance

```
┌───────────────────────┬────────────────────────────────────────────────────┐
│ ▣ Unmatched      3    │  GE Optima CT520                    [Delete scanner]│
│                       │  Display name   RHYTHM scanner ID                   │
│ SCANNERS              │  Manufacturer   Model                               │
│  Siemens Emotion 16 7 │  ─────────────────────────────────────────────────  │
│  GE Optima CT520    1 │  Protocol │ Matches names │ Region/indication │ UUID │
│                       │  CAP …    │ CAP_SPIRAL_…  │ Chest — …         │ …    │
│ [Add scanner]         │  [Add protocol]                                     │
└───────────────────────┴────────────────────────────────────────────────────┘
                                         Unsaved changes  [Discard] [Save changes]
```

- **Left:** the **Unmatched** inbox (studies nothing matches yet), then the list of
  scanners.
- **Right:** the selected scanner's details and its protocol table, or the inbox.
- **Bottom:** a save bar appears as soon as something is changed. Nothing takes
  effect until **Save changes** is clicked.

**Who can edit:** admins only. Other logged-in users see the same page read-only.

---

## How a study is matched

Matching happens automatically when a study finishes arriving, and again over all
waiting studies whenever the catalog is saved.

1. **Scanner.** The study's DICOM *Manufacturer* and *Manufacturer Model Name* are
   compared with each scanner's **Manufacturer** and **Model**. The comparison
   ignores upper/lower case and must otherwise match exactly.
2. **Protocol.** Within that scanner, the study's protocol name is compared with
   each protocol's **names**. The comparison ignores case and extra spaces. The
   protocol name is taken from:
   - DICOM *ProtocolName*, or, when that is empty,
   - the protocol part of *StudyDescription*: the text after the last `^`, without
     a trailing `( … )`.

   | StudyDescription                    | Protocol name used     |
   |-------------------------------------|------------------------|
   | `Head^CAP_SPIRAL_CONTRAST (Adult)`  | `CAP_SPIRAL_CONTRAST`  |
   | `Thorax^ThoraxRoutine`              | `ThoraxRoutine`        |
   | `ThoraxRoutine (Child)`             | `ThoraxRoutine`        |

3. **"Any scanner" groups.** A scanner whose manufacturer **and** model are both
   blank matches studies from every scanner. It is tried after the study's own
   scanner, which is useful for protocols named the same everywhere.

The same protocol name on two different scanners stays two separate protocols, each
with its own RHYTHM UUID. That is intended, because RHYTHM registers protocols per
scanner.

### What a match fills in

| Study field                          | Filled from the protocol | Notes |
|--------------------------------------|--------------------------|-------|
| RHYTHM protocol                      | always                   | its UUID becomes the upload's `protocol_id` |
| Region / clinical indication         | when set on the protocol | |
| IV contrast                          | when set on the protocol | otherwise guessed from the images (contrast agent tag) |
| Scanner (name, RHYTHM scanner ID)    | from the scanner         | display only |

**Manual edits always win.** If an operator chose a value on `/studies`, matching
never overwrites it. If a protocol is deleted or renamed so a study no longer
matches, the values that match had filled in are cleared again. Hand-entered values
stay.

Studies that are already **exported** are never changed.

---

## Everyday tasks

### A new scanner starts sending studies

1. Open **Protocols → Unmatched**. The studies show up grouped as
   *unknown* `GE MEDICAL SYSTEMS | Optima CT520 Series` with their protocol names.
2. Click **Add scanner + protocol**. This creates the scanner with the exact
   manufacturer/model and a first protocol named after the study's protocol, then
   opens the new scanner.
3. Fill in, for the protocol:
   - **Region / indication**: pick one of RHYTHM's 8 combinations. Contrast is set
     to that combination's default and can be changed.
   - **RHYTHM protocol UUID**: from RHYTHM's *Protocol Records* page.
4. **Save changes.** A notice says how many studies were re-matched.

### A known scanner sends a new protocol name

In **Unmatched**, for that row, do one of these:

- **New protocol** if it really is a different protocol; then fill in its details as above.
- **Add as alias of…** if it's another name for an existing protocol (e.g. the
  technologists renamed it on the console). The name is added to that protocol's
  *Matches names* list.

Then **Save changes**.

### Adding things by hand

- **Add scanner** (left column) asks for the manufacturer and model. Enter them
  exactly as the DICOM header has them. Leave both blank for an "any scanner" group.
- **Add protocol** (below a scanner's table) adds an empty row. Typing the
  **Protocol** label also sets its matching name and internal key.
- **Matches names**: type a name and press Enter to add it; click × on a name to
  remove it.

### Setting or changing a RHYTHM UUID

Paste it into the protocol's **RHYTHM protocol UUID** field and save. Until a
protocol has a UUID it shows **UUID pending — export blocked**: studies still match
it and pre-fill, but RHYTHM upload refuses them with a message pointing here.

### Draft a protocol from a study (creates it on RHYTHM)

For a study whose protocol isn't registered anywhere yet: **Draft from study NNNN**
in the Unmatched inbox, or **Draft protocol** on the study in `/studies` (admins).
PixieVeil pre-fills both halves from the study:

- **Local protocol:** matching name(s) from ProtocolName / StudyDescription,
  region / indication / contrast, and the scanner (an existing one, or a new one
  with the study's DICOM manufacturer and model).
- **RHYTHM protocol**, every field limited to RHYTHM's own options (read live from
  RHYTHM's protocol-choices list, or a bundled copy when RHYTHM is unreachable).
  Each field is badged:
  - *from study* (green): read from the dose report (RDSR) or image headers.
    That covers kVp, pitch, rotation time, scan type, number of phases, slice
    thickness of the first reconstruction, contrast, and the age/weight group
    from the patient's age and weight;
  - *guess* (amber): heuristics to check. That covers the protocol type,
    examination group, mA modulation (for example XYZ_EC → CARE Dose4D), kernel
    class and reconstruction algorithm from the kernel (`H31s` → soft tissue,
    FBP; `Br40f` + strength → ADMIRE), scan FOV, and the QR mAs (estimated as
    the effective mAs: mean mA × rotation ÷ pitch — **replace it with the
    console's reference value**);
  - *please fill* (red): not in the images, for example automatic kVp selection
    (CARE kV).

  Values outside RHYTHM's list become *Other: Please Specify* with the measured
  value.
- **Similar protocols already on RHYTHM** (same scanner and type, same name or age
  group) are listed with **Use this one**, which links that UUID instead of
  creating a duplicate.

**Create on RHYTHM & add locally** checks everything first, then creates the
protocol through RHYTHM's API with your token, and adds or updates the local
protocol with the new UUID. Matching studies are re-matched straight away. If
the local save fails after RHYTHM accepted the protocol, the message gives the
new UUID to paste here.

> RHYTHM's API (as of our vendored copy) does not store the **examination
> group** — only its own website does. If the result says so, open the protocol on
> RHYTHM's Protocol Records page and set the examination group there.

One draft makes one RHYTHM record (one protocol type and age/weight group). Draft
again from a study in another age group for the next one.

### Import from RHYTHM

Once the RHYTHM connection is set on **Settings** (base URL + API token, see
[RHYTHM_API.md](RHYTHM_API.md)), **Import from RHYTHM** (left column) reads your
site's scanner profiles and protocols from RHYTHM and proposes how they map onto
this page. Nothing changes until you tick proposals, click **Apply selected** and
then **Save changes**. Start from a saved page: the button refuses while there are
unsaved changes.

- **Scanners** pair by manufacturer and model. RHYTHM's "Siemens Healthineers /
  SOMATOM Emotion 16" pairs with the DICOM "SIEMENS / Emotion 16". Only an exact
  model match pairs automatically; otherwise pick the local scanner in the dialog,
  or *skip*. Pairing also fills the scanner's *RHYTHM scanner ID* if it's empty.
- **Protocols** pair first by UUID: a local protocol that already carries the RHYTHM
  protocol's UUID is its match, whatever its names. Otherwise they pair by name,
  ignoring case and treating `_`, `-` and spaces alike (`CAP_SPIRAL_CONTRAST` =
  "cap spiral contrast").
- **Scanner names** column: type the ProtocolName(s) your scanner sends for that
  RHYTHM protocol, comma-separated (for example `CAP_N_1-6_ANI`). They usually
  differ from RHYTHM's protocol name, and they're what matches incoming studies.
  They're added to the matched protocol's *Matches names*, or become the names of
  a new protocol. Existing names are kept.

  | Proposal | Meaning | Ticked by default |
  |---|---|---|
  | Set UUID | the local protocol has no UUID yet | yes |
  | Already set | same UUID; shown for completeness | — |
  | Replace UUID | the local protocol has a *different* UUID (flagged when that UUID is shared by several protocols, as after the upgrade) | no |
  | New protocol | no local protocol has that name; adds one under the chosen scanner | no |
  | Pick protocol | several local protocols have that name; choose which | no |

- **Region / indication / contrast** are copied only into empty fields, and only
  when RHYTHM's values are one of the known combinations. Differences are shown as
  notes, never overwritten.
- Also listed: local scanners and protocols that RHYTHM doesn't have yet (register
  them there), and RHYTHM protocols with another site code (skipped).

### Deleting

- **Delete** on a protocol row. The *Studies* column shows how many studies use it.
  Those studies lose the auto-filled protocol and indication on save, unless an
  operator set them by hand.
- **Delete scanner** removes the scanner and all its protocols.

---

## Field reference

**Scanner**

| Field | Meaning |
|---|---|
| Display name | shown on the dashboard |
| Manufacturer / Model | DICOM `Manufacturer` / `ManufacturerModelName`, exactly as the scanner sends them. Both blank means an "any scanner" group |
| RHYTHM scanner ID | optional, for reference. RHYTHM takes the scanner from the protocol |

**Protocol**

| Field | Meaning |
|---|---|
| Protocol (label) | display name; the grey text under it is the internal key (stored on each study, kept stable) |
| Matches names | exact protocol names that select this protocol (one or more) |
| Region / indication | one of RHYTHM's 8 region + clinical-indication combinations, or empty |
| Contrast | `Non-contrast` / `Contrast-enhanced`, or empty |
| RHYTHM protocol UUID | the registered RHYTHM *CTProtocol* UUID; empty = pending |
| Studies | how many studies currently use this protocol |

### Rules checked on save

The page refuses to save, and lists the problems at the top, when:

- two scanners have the same manufacturer + model;
- a name appears twice on the same scanner;
- a UUID isn't a valid UUID;
- a region/indication isn't one of RHYTHM's combinations;
- the catalog's site code doesn't match `storage.remote_storage.rhythm.site_code`.

If someone else saved in the meantime, the page asks you to reload instead of
overwriting their changes.

---

## Where it is stored

Everything lives in **`config/protocols.yaml`**. Another path can be set with
`storage.protocols_file` in `config/settings.yaml`. The page writes the file
atomically, and the server re-reads it if it is changed on disk. Editing it by
hand is possible but not needed. See
[`config/protocols.example.yaml`](config/protocols.example.yaml) for the format.

### Upgrading from the old lookup files

Before this page existed, the same information was split across:

- `integrations/rhythm/scanner_lookup.yaml` (scanners)
- `integrations/rhythm/indication_lookup.yaml` (indication rules)
- `config/rhythm_protocols.yaml` (protocol → RHYTHM UUID)

On the first start without `config/protocols.yaml`, PixieVeil builds it from those
files and logs a warning. The old files are then no longer read; they carry a
*RETIRED* header.

- **Protocol keys are kept**, so studies that already had a RHYTHM protocol still
  resolve.
- **Protocols go under the one scanner** when there was exactly one. Otherwise they
  go into an **"Any scanner"** group to sort out on the page.
- **Region / indication** is copied from an old indication rule only when the rule
  named that exact protocol. Other rules are listed in the log and must be recreated
  on the page.

**After upgrading:**

1. Check the migrated UUIDs. Every protocol needs its *own* RHYTHM protocol UUID.
2. Save the page once, even with no changes. This re-matches studies processed
   before the upgrade.

---

## API

| Request | Who | What |
|---|---|---|
| `GET /api/protocols` | any user | the catalog, its `version`, per-protocol study counts, `editable` |
| `PUT /api/protocols` | admin | body `{"catalog": {...}, "version": "<from GET>"}`; `409` if changed since, `400` with `errors` if invalid; on success re-matches waiting studies and returns them as `restudied` |
| `GET /api/protocols/unmatched` | any user | unmatched studies grouped by manufacturer, model and protocol name |
| `POST /api/protocols/rhythm-import` | admin | reads RHYTHM's scanner profiles + protocols with the saved token and returns the import `plan`; writes nothing (`409` if RHYTHM isn't configured, `502` with RHYTHM's error) |
| `GET /api/studies/{n}/protocol-draft` | admin | the pre-filled local + RHYTHM draft for study n, RHYTHM's options (`choices_source` live/offline), scanner profiles, similar RHYTHM protocols |
| `POST /api/studies/{n}/protocol-draft` | admin | `{local, rhythm}` or `{local, use_existing}`: validates, creates the protocol on RHYTHM, adds the local one with its UUID |

These need a login or an API token, like the rest of the dashboard (see
[USERS.md](USERS.md)).
