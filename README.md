# fbx-clip-extractor

Slice multi-take / Unity-style FBX files into individual animation clips, optionally bundle them as multi-take FBX files. Built for s&box / Source 2 ModelDoc, but the output works just as well in Unreal, Unity, or any FBX-aware tool.

## What it solves

Unity asset packs usually ship one long FBX timeline per file plus a `.meta` sidecar that defines clip ranges via `firstFrame` / `lastFrame`. Blender, and most engines, see this as one giant animation. This script reads the sidecar, slices the master action, and writes each clip out as a clean, standalone FBX or as a take inside a bundled FBX.

## Requirements

- Blender 4.0+
- Python (bundled with Blender — no `pip install` needed)

## How it picks a mode

Auto-selected per input file:

1. **Unity-meta mode**: if `<input>.meta` exists with a `clipAnimations:` block, each clip's `firstFrame` / `lastFrame` is sliced out of the master action.
2. **Action mode**: if no usable meta sidecar is found, every action already in the FBX is exported as-is.

Force action mode with `--no-meta`. Override the sidecar path with `--meta PATH` (single-input only).

## Run it

```
blender --background --python extract_animations.py -- [args]
```

Everything after `--` is forwarded to the script.

## Arguments

| Flag | Type | What it does |
|------|------|--------------|
| `--input PATH [PATH ...]` | required | One or more source FBX files. |
| `--output DIR` | required | Output root directory. |
| `--prefix STR` | optional | String prefix for per-clip filenames. |
| `--armature-only` | flag | Drop meshes from exports — skeleton + animation only. |
| `--meta PATH` | optional | Explicit `.meta` sidecar path (single-input only). |
| `--no-meta` | flag | Ignore `.meta` sidecars; use action mode. |
| `--combined` | flag | Per source: also write `<source>-Clips.fbx` (every clip as a take). |
| `--combined-only` | flag | Skip per-clip files; only write the per-source bundle. |
| `--all-combined` | flag | Across all inputs: also write `All-Clips.fbx` (every clip as a take). |
| `--all-combined-only` | flag | Skip the per-source pass entirely; only write `All-Clips.fbx`. |

## Examples

### 1. Per-clip files (one source)

**Input**
```
Animations/
├── Defenses.fbx          (one master action with 26 takes)
└── Defenses.fbx.meta     (Unity sidecar with 26 clip ranges)
```

**Command**
```
blender --background --python extract_animations.py -- \
    --input  Animations/Defenses.fbx \
    --output extracted
```

**Output**
```
extracted/
└── Defenses/
    ├── High-Guard.fbx
    ├── Low-Guard.fbx
    ├── Slip-Backward-Idle.fbx
    └── ... (23 more)
```

### 2. Per-clip + per-source bundle

Add `--combined`:

```
blender --background --python extract_animations.py -- \
    --input  Animations/Defenses.fbx \
    --output extracted \
    --combined
```

**Output**
```
extracted/
└── Defenses/
    ├── High-Guard.fbx
    ├── ...                       (26 individual clips)
    └── Defenses-Clips.fbx        (all 26 takes in one FBX)
```

### 3. Per-source bundle only (skip individuals)

Use `--combined-only` to skip the per-clip files:

```
blender --background --python extract_animations.py -- \
    --input  Animations/Defenses.fbx \
    --output extracted \
    --combined-only
```

**Output**
```
extracted/
└── Defenses/
    └── Defenses-Clips.fbx        (only the bundle; no individual clips)
```

### 4. Many sources → one mega-bundle

**Input**
```
Animations/
├── Defenses.fbx     + Defenses.fbx.meta       (26 clips)
├── Hits.fbx         + Hits.fbx.meta           (6  clips)
├── Locomotion.fbx   + Locomotion.fbx.meta     (8  clips)
├── Punches.fbx      + Punches.fbx.meta        (90 clips)
├── Stance.fbx       + Stance.fbx.meta         (2  clips)
└── Steps.fbx        + Steps.fbx.meta          (8  clips)
```

**Command** (note: `--input` accepts multiple paths)
```
blender --background --python extract_animations.py -- \
    --input  Animations/Defenses.fbx Animations/Hits.fbx Animations/Locomotion.fbx \
             Animations/Punches.fbx  Animations/Stance.fbx Animations/Steps.fbx \
    --output extracted \
    --all-combined-only
```

**Output** (one file, 140 takes inside)
```
extracted/
└── All-Clips.fbx
```

Drop `All-Clips.fbx` into ModelDoc / Unity / Unreal and every clip shows up as its own animation.

### 5. The full layout (everything at once)

Combine `--combined` + `--all-combined` for per-clip files, per-source bundles, and a mega-bundle in one run:

```
blender --background --python extract_animations.py -- \
    --input  Animations/*.fbx \
    --output extracted \
    --combined --all-combined
```

**Output**
```
extracted/
├── All-Clips.fbx                 (140 takes from every source)
├── Defenses/
│   ├── High-Guard.fbx
│   ├── ...                       (26 per-clip files)
│   └── Defenses-Clips.fbx        (26-take bundle)
├── Hits/
│   ├── Hit-Head-Front.fbx
│   ├── ...
│   └── Hits-Clips.fbx
└── ... (Locomotion, Punches, Stance, Steps — same shape)
```

## PowerShell convenience snippet

```powershell
$blender = "C:\Program Files\Blender Foundation\Blender 4.4\blender.exe"
$script  = "C:\path\to\fbx-clip-extractor\extract_animations.py"
$src     = "C:\path\to\Animations"
$dst     = "C:\path\to\extracted"

& $blender --background --python $script -- `
    --input (Get-ChildItem "$src\*.fbx" | ForEach-Object FullName) `
    --output $dst `
    --combined --all-combined
```

## Tweakable export defaults

Constants near the top of `extract_animations.py` control the FBX export:

| Constant | Default | Notes |
|----------|---------|-------|
| `EXPORT_AXIS_FORWARD` / `EXPORT_AXIS_UP` | `-Z` / `Y` | Standard FBX axes. |
| `EXPORT_PRIMARY_BONE_AXIS` / `EXPORT_SECONDARY_BONE_AXIS` | `X` / `Z` | Required by s&box ModelDoc. |
| `EXPORT_ADD_LEAF_BONES` | `False` | Required by s&box ModelDoc. |
| `EXPORT_FRAME_RATE` | `30` | Set `None` to keep the imported scene's fps. |
| `EXPORT_BAKE_STEP` | `1.0` | Sample every frame. |
| `EXPORT_SIMPLIFY_FACTOR` | `0.0` | `0` = no key reduction (preserves mocap fidelity). |

## Notes

- Clip names like `1.High-Guard` from Unity get their leading `N.` stripped automatically (becomes `High-Guard`).
- The `.meta` sidecar is 0-indexed; Blender's import is usually 1-indexed. The script auto-detects and applies the offset.
- In `--all-combined` mode, name collisions across sources get a `<source>_<clip>` prefix. Clean clip names always win when unique.
- All inputs share the first source's armature for the bundled export; safe as long as bone names match across files (they do for any pack built on a single Mecanim humanoid rig).
