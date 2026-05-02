"""
Extract every animation from one or more FBX files into individual FBX files,
optionally also packaging them as multi-take FBX bundles.

Two modes are auto-selected per input file:

  1. Unity-meta mode  — if a `<input>.meta` file sits next to the FBX with a
     `clipAnimations:` list, each clip's `firstFrame`/`lastFrame` range is
     sliced out of the master action and exported as its own FBX. Use this
     for Unity Asset-Store packs (Mixamo, Punch Perfect, etc.) that ship one
     long timeline per FBX with clip ranges defined in the meta file.

  2. Action mode  — if no usable meta file is found, every entry in
     `bpy.data.actions` is exported individually (one FBX per action).

Output layout:
    <output>/<source-stem>/<clip>.fbx           per-clip files
    <output>/<source-stem>/<source-stem>-Clips.fbx   per-source bundle  (--combined)
    <output>/All-Clips.fbx                      everything in one bundle (--all-combined)

Usage examples:
    # one source, per-clip files only
    blender --background --python extract_animations.py -- \
        --input "C:/anims/Defenses.fbx" --output "C:/out"

    # one source, also write a per-source bundle
    blender --background --python extract_animations.py -- \
        --input "C:/anims/Defenses.fbx" --output "C:/out" --combined

    # six sources, per-clip + per-source bundles + one All-Clips.fbx
    blender --background --python extract_animations.py -- \
        --input "C:/anims/Defenses.fbx" "C:/anims/Hits.fbx" "C:/anims/Locomotion.fbx" \
                "C:/anims/Punches.fbx"  "C:/anims/Stance.fbx" "C:/anims/Steps.fbx" \
        --output "C:/out" --combined --all-combined

Requires Blender 4.0+. No dependencies beyond bundled `bpy`.
"""

import bpy
import os
import sys
import re
import argparse
import traceback

# ---------------------------------------------------------------------------
# FBX export settings — defaults tuned for s&box / Source 2 ModelDoc.
# ---------------------------------------------------------------------------
EXPORT_AXIS_FORWARD          = "-Z"
EXPORT_AXIS_UP               = "Y"
EXPORT_GLOBAL_SCALE          = 1.0
EXPORT_APPLY_UNIT_SCALE      = True
EXPORT_APPLY_SCALE_OPTIONS   = "FBX_SCALE_NONE"

EXPORT_PRIMARY_BONE_AXIS     = "X"
EXPORT_SECONDARY_BONE_AXIS   = "Z"
EXPORT_ADD_LEAF_BONES        = False
EXPORT_ARMATURE_NODETYPE     = "NULL"
EXPORT_USE_ARMATURE_DEFORM   = False

EXPORT_BAKE_STEP             = 1.0
EXPORT_SIMPLIFY_FACTOR       = 0.0
EXPORT_FORCE_STARTEND_KEYING = True
EXPORT_FRAME_RATE            = 30


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []

    parser = argparse.ArgumentParser(
        description="Extract animations from FBX files into separate / bundled FBX files.",
    )
    parser.add_argument("--input", nargs="+", required=True,
                        help="One or more source FBX file paths.")
    parser.add_argument("--output", required=True, help="Output directory.")
    parser.add_argument("--prefix", default="", help="Filename prefix for per-clip exports.")
    parser.add_argument("--armature-only", action="store_true",
                        help="Export only the armature/skeleton (skip meshes).")
    parser.add_argument("--meta", default=None,
                        help="Explicit Unity .meta sidecar path. Only valid with a single --input.")
    parser.add_argument("--no-meta", action="store_true",
                        help="Ignore .meta sidecars and use per-action mode.")
    parser.add_argument("--combined", action="store_true",
                        help="Per source: also write <source-stem>-Clips.fbx with every clip as a take.")
    parser.add_argument("--combined-only", action="store_true",
                        help="Skip per-clip files; only emit the per-source bundle (implies --combined).")
    parser.add_argument("--all-combined", action="store_true",
                        help="Also write <output>/All-Clips.fbx with every clip from every input as a take.")
    parser.add_argument("--all-combined-only", action="store_true",
                        help="Skip the per-source pass entirely; only emit All-Clips.fbx.")
    return parser.parse_args(argv)


def sanitize_filename(name: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F\s]+', "_", name)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned or "unnamed"


def clean_clip_name(raw: str) -> str:
    """Strip leading 'N.' Unity-style numbering."""
    return re.sub(r"^\d+\.\s*", "", raw).strip()


# ---------------------------------------------------------------------------
# Unity .meta parsing
# ---------------------------------------------------------------------------

def parse_unity_meta(meta_path):
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    section_match = re.search(r"\n\s*clipAnimations:\s*\n", text)
    if not section_match:
        return []
    section_start = section_match.end()
    rest = text[section_start:]
    end_match = re.search(r"\n  [A-Za-z]", rest)
    section = rest[: end_match.start()] if end_match else rest

    clips = []
    clip_pattern = re.compile(
        r"- serializedVersion: \d+.*?(?=\n    - serializedVersion:|\Z)",
        re.DOTALL,
    )
    for block_match in clip_pattern.finditer(section):
        block = block_match.group(0)
        name_m  = re.search(r"^\s*name:\s*(.+?)\s*$",      block, re.MULTILINE)
        first_m = re.search(r"^\s*firstFrame:\s*(\S+)\s*$", block, re.MULTILINE)
        last_m  = re.search(r"^\s*lastFrame:\s*(\S+)\s*$",  block, re.MULTILINE)
        if not (name_m and first_m and last_m):
            continue
        try:
            first = int(round(float(first_m.group(1))))
            last  = int(round(float(last_m.group(1))))
        except ValueError:
            continue
        clips.append((name_m.group(1).strip(), first, last))
    return clips


# ---------------------------------------------------------------------------
# Scene helpers
# ---------------------------------------------------------------------------

def clear_scene():
    if bpy.data.objects:
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
    for collection in (
        bpy.data.actions, bpy.data.armatures, bpy.data.meshes,
        bpy.data.materials, bpy.data.images, bpy.data.cameras, bpy.data.lights,
    ):
        for item in list(collection):
            collection.remove(item)


def find_armature():
    for obj in bpy.data.objects:
        if obj.type == "ARMATURE":
            return obj
    return None


def clear_nla(armature):
    if armature.animation_data and armature.animation_data.nla_tracks:
        for track in list(armature.animation_data.nla_tracks):
            armature.animation_data.nla_tracks.remove(track)


def assign_action_via_nla(armature, action, strip_start_frame=0):
    if armature.animation_data is None:
        armature.animation_data_create()
    clear_nla(armature)
    armature.animation_data.action = action
    track = armature.animation_data.nla_tracks.new()
    track.name = action.name
    track.strips.new(name=action.name, start=int(strip_start_frame), action=action)


def slice_action(source_action, new_name, start_frame, end_frame):
    """Build a fresh action containing the source's evaluated values across
    [start, end], shifted so the clip begins at frame 0."""
    if new_name in bpy.data.actions:
        # Force-unique within this Blender session.
        suffix = 1
        while f"{new_name}.{suffix:03d}" in bpy.data.actions:
            suffix += 1
        new_name = f"{new_name}.{suffix:03d}"

    new_action = bpy.data.actions.new(name=new_name)
    new_action.use_fake_user = True
    duration = max(end_frame - start_frame, 1)

    for src_fc in source_action.fcurves:
        new_fc = new_action.fcurves.new(
            data_path=src_fc.data_path,
            index=src_fc.array_index,
            action_group=src_fc.group.name if src_fc.group else "",
        )
        for frame in range(start_frame, end_frame + 1):
            value = src_fc.evaluate(frame)
            kp = new_fc.keyframe_points.insert(
                frame=frame - start_frame, value=value, options={"FAST"},
            )
            kp.interpolation = "LINEAR"
        new_fc.update()

    return new_action, duration


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------

def _select_for_export(armature, armature_only):
    object_types = {"ARMATURE"} if armature_only else {"ARMATURE", "MESH", "EMPTY"}
    bpy.ops.object.select_all(action="DESELECT")
    selected_any = False
    for obj in bpy.data.objects:
        if obj.type in object_types:
            obj.select_set(True)
            selected_any = True
    if not selected_any:
        raise RuntimeError("No objects of the requested types are present.")
    bpy.context.view_layer.objects.active = armature
    return object_types


def _common_fbx_kwargs():
    return dict(
        global_scale=EXPORT_GLOBAL_SCALE,
        apply_unit_scale=EXPORT_APPLY_UNIT_SCALE,
        apply_scale_options=EXPORT_APPLY_SCALE_OPTIONS,
        axis_forward=EXPORT_AXIS_FORWARD,
        axis_up=EXPORT_AXIS_UP,
        primary_bone_axis=EXPORT_PRIMARY_BONE_AXIS,
        secondary_bone_axis=EXPORT_SECONDARY_BONE_AXIS,
        add_leaf_bones=EXPORT_ADD_LEAF_BONES,
        armature_nodetype=EXPORT_ARMATURE_NODETYPE,
        use_armature_deform_only=EXPORT_USE_ARMATURE_DEFORM,
        bake_anim=True,
        bake_anim_use_all_bones=True,
        bake_anim_force_startend_keying=EXPORT_FORCE_STARTEND_KEYING,
        bake_anim_step=EXPORT_BAKE_STEP,
        bake_anim_simplify_factor=EXPORT_SIMPLIFY_FACTOR,
        path_mode="AUTO",
    )


def export_single_clip(armature, action, output_path, frame_start, frame_end, armature_only):
    scene = bpy.context.scene
    scene.frame_start = int(frame_start)
    scene.frame_end   = max(int(frame_end), int(frame_start) + 1)
    if EXPORT_FRAME_RATE is not None:
        scene.render.fps = EXPORT_FRAME_RATE
        scene.render.fps_base = 1.0

    object_types = _select_for_export(armature, armature_only)
    assign_action_via_nla(armature, action,
                          strip_start_frame=int(round(action.frame_range[0])))

    bpy.ops.export_scene.fbx(
        filepath=output_path,
        use_selection=True,
        object_types=object_types,
        bake_anim_use_nla_strips=True,
        bake_anim_use_all_actions=False,
        **_common_fbx_kwargs(),
    )


def export_bundle(armature, actions, output_path, armature_only, prime=True):
    """Export a single FBX with every action in `actions` as its own take.

    `bake_anim_use_all_actions=True` only bakes the actual action data when
    Blender has previously evaluated the action against the armature via a
    real export pass; otherwise it silently emits rest pose. When the caller
    cannot guarantee that has already happened (e.g. the all-combined path),
    pass `prime=True` to force a throwaway per-action export pass first."""
    import tempfile

    keep = {a.name for a in actions}
    for a in list(bpy.data.actions):
        if a.name not in keep:
            bpy.data.actions.remove(a)

    if armature.animation_data is None:
        armature.animation_data_create()
    clear_nla(armature)

    if prime:
        with tempfile.TemporaryDirectory() as tmp:
            for i, action in enumerate(actions):
                tmp_path = os.path.join(tmp, f"_prime_{i}.fbx")
                try:
                    export_single_clip(
                        armature, action, tmp_path,
                        int(round(action.frame_range[0])),
                        int(round(action.frame_range[1])),
                        armature_only,
                    )
                except Exception as e:
                    print(f"[WARN]   Prime pass failed for '{action.name}': {e}")

    clear_nla(armature)
    armature.animation_data.action = actions[0]

    object_types = _select_for_export(armature, armature_only)

    scene = bpy.context.scene
    longest = max(int(round(a.frame_range[1])) for a in actions)
    scene.frame_start = 0
    scene.frame_end   = max(longest, 1)
    if EXPORT_FRAME_RATE is not None:
        scene.render.fps = EXPORT_FRAME_RATE
        scene.render.fps_base = 1.0

    bpy.ops.export_scene.fbx(
        filepath=output_path,
        use_selection=True,
        object_types=object_types,
        bake_anim_use_nla_strips=False,
        bake_anim_use_all_actions=True,
        **_common_fbx_kwargs(),
    )


# ---------------------------------------------------------------------------
# Per-source pass (writes per-clip files and optionally a per-source bundle)
# ---------------------------------------------------------------------------

def build_clip_actions(input_path, master_action=None, meta_override=None, no_meta=False):
    """After importing `input_path`, slice the master action into clip actions.
    Returns (armature, [(clip_name, action, first, last), ...], used_meta).

    `master_action` should be the freshly-imported source action; if None, the
    first entry in bpy.data.actions is used (correct only when the scene was
    cleared right before the import)."""
    armature = find_armature()
    if armature is None:
        raise RuntimeError("No armature found in source FBX.")

    meta_path = None
    if not no_meta:
        meta_path = meta_override or (input_path + ".meta")
        if not os.path.isfile(meta_path):
            meta_path = None

    unity_clips = parse_unity_meta(meta_path) if meta_path else []
    built = []
    used_meta = False

    if unity_clips:
        used_meta = True
        if master_action is None:
            master_actions = list(bpy.data.actions)
            if not master_actions:
                raise RuntimeError("No actions imported — cannot slice clips.")
            master = master_actions[0]
        else:
            master = master_action
        master_start = int(round(master.frame_range[0]))
        master_end   = int(round(master.frame_range[1]))
        unity_min    = min(c[1] for c in unity_clips)
        frame_offset = master_start - unity_min
        print(f"[INFO]   Master '{master.name}' spans {master_start}..{master_end}; "
              f"{len(unity_clips)} clip(s); frame offset {frame_offset:+d}")

        for raw_name, first, last in unity_clips:
            clip_name = clean_clip_name(raw_name)
            first += frame_offset
            last  += frame_offset
            if last <= first:
                print(f"[WARN]   Skipping '{clip_name}' (invalid range)")
                continue
            if first < master_start or last > master_end:
                first = max(first, master_start)
                last  = min(last, master_end)
                if last <= first:
                    continue
            try:
                clip_action, _duration = slice_action(master, clip_name, first, last)
            except Exception as e:
                print(f"[FAIL]   Could not slice '{clip_name}': {e}")
                continue
            built.append((clip_name, clip_action, 0, int(round(clip_action.frame_range[1]))))

        # The master action is no longer needed.
        if master.name in bpy.data.actions:
            bpy.data.actions.remove(master)
    else:
        actions = list(bpy.data.actions)
        if not actions:
            return armature, [], False
        print(f"[INFO]   No usable .meta — per-action mode ({len(actions)} action(s))")
        for action in actions:
            if len(action.fcurves) == 0:
                print(f"[WARN]   Skipping '{action.name}' (zero fcurves)")
                continue
            action.use_fake_user = True
            start, end = action.frame_range
            built.append((clean_clip_name(action.name), action,
                          int(round(start)), int(round(end))))

    return armature, built, used_meta


def process_source(input_path, output_root, args):
    """Per-source pass: writes per-clip files + optional per-source bundle."""
    source_stem = os.path.splitext(os.path.basename(input_path))[0]
    source_out  = os.path.join(output_root, source_stem)
    os.makedirs(source_out, exist_ok=True)

    print(f"\n[INFO] === {source_stem}.fbx ===")
    clear_scene()
    bpy.ops.import_scene.fbx(filepath=input_path, use_anim=True)

    armature, built, used_meta = build_clip_actions(
        input_path,
        meta_override=args.meta if len(args.input) == 1 else None,
        no_meta=args.no_meta,
    )

    if not built:
        print(f"[INFO]   No clips to export for {source_stem}")
        return 0, 0

    write_bundle    = args.combined or args.combined_only
    write_per_clip  = not args.combined_only

    success = 0
    failed = []
    used_names = set()
    total = len(built)

    if write_per_clip:
        for index, (clip_name, action, first, last) in enumerate(built, start=1):
            tag = f"[{index}/{total}]"
            base = sanitize_filename(clip_name)
            candidate = f"{args.prefix}{base}"
            unique = candidate
            suffix = 1
            while unique.lower() in used_names:
                unique = f"{candidate}_{suffix:03d}"
                suffix += 1
            used_names.add(unique.lower())
            out_path = os.path.join(source_out, f"{unique}.fbx")

            try:
                export_single_clip(armature, action, out_path, first, last, args.armature_only)
                success += 1
            except Exception as e:
                print(f"[FAIL] {tag} '{clip_name}': {e}")
                failed.append(clip_name)
        print(f"[SUMMARY]   {success}/{total} per-clip FBX written to {source_out}")

    if write_bundle:
        bundle_path = os.path.join(source_out, f"{source_stem}-Clips.fbx")
        actions = [b[1] for b in built]
        try:
            # Per-clip exports above already primed the actions; skip the
            # heavy prime pass when they ran.
            export_bundle(armature, actions, bundle_path, args.armature_only,
                          prime=not write_per_clip)
            print(f"[SUMMARY]   Bundle written: {bundle_path}")
        except Exception as e:
            print(f"[FAIL]   Bundle failed: {e}")
            traceback.print_exc()

    if failed:
        print(f"[SUMMARY]   Failed clips: {failed}")

    return success, total


# ---------------------------------------------------------------------------
# All-combined pass (one FBX with takes from every input)
# ---------------------------------------------------------------------------

def export_all_combined(inputs, output_path, args):
    """Reset scene, import every input in turn, slice clips, keep only the
    first armature, then export one FBX with every clip as a take."""
    print(f"\n[INFO] === Building All-Clips bundle ===")
    clear_scene()

    canonical_armature = None
    all_actions = []
    all_clip_names = set()

    for input_path in inputs:
        source_stem = os.path.splitext(os.path.basename(input_path))[0]
        print(f"[INFO]   Importing {source_stem}.fbx")

        armatures_before = {o.name for o in bpy.data.objects if o.type == "ARMATURE"}
        meshes_before    = {o.name for o in bpy.data.objects if o.type == "MESH"}
        actions_before   = {a.name for a in bpy.data.actions}

        try:
            bpy.ops.import_scene.fbx(filepath=input_path, use_anim=True)
        except Exception as e:
            print(f"[FAIL]   Import failed for {input_path}: {e}")
            continue

        new_actions = [a for a in bpy.data.actions if a.name not in actions_before]
        if not new_actions:
            print(f"[WARN]   {source_stem}.fbx contributed no new actions; skipping")
            continue
        master_action = new_actions[0]

        _, built, _used_meta = build_clip_actions(
            input_path,
            master_action=master_action,
            meta_override=None,
            no_meta=args.no_meta,
        )

        # Disambiguate clip names across sources by prefixing the source stem
        # only on collision; otherwise keep the clean name.
        for clip_name, action, first, last in built:
            final_name = clip_name
            if final_name in all_clip_names:
                final_name = f"{source_stem}_{clip_name}"
                base = final_name
                n = 1
                while final_name in all_clip_names:
                    n += 1
                    final_name = f"{base}.{n:03d}"
            all_clip_names.add(final_name)
            if action.name != final_name:
                action.name = final_name
            action.use_fake_user = True
            all_actions.append(action)

        # Keep the first armature alive, drop all subsequently-imported ones
        # Drop later sources' armatures + meshes, but keep the canonical
        # source's armature AND its meshes. The meshes carry the Armature
        # modifier that forces pose-bone evaluation during bake; deleting
        # them makes the FBX exporter silently emit rest pose.
        new_armatures = [o for o in bpy.data.objects
                         if o.type == "ARMATURE" and o.name not in armatures_before]
        new_meshes    = [o for o in bpy.data.objects
                         if o.type == "MESH" and o.name not in meshes_before]

        first_iteration = canonical_armature is None
        if first_iteration and new_armatures:
            canonical_armature = new_armatures[0]
            new_armatures = new_armatures[1:]

        # On the first iteration we keep this source's meshes so the canonical
        # armature has skinned geometry for pose evaluation.
        meshes_to_drop = [] if first_iteration else new_meshes
        for obj in new_armatures + meshes_to_drop:
            if obj.name in bpy.data.objects:
                bpy.data.objects.remove(obj, do_unlink=True)

    if canonical_armature is None:
        print("[ERROR]   No armature available for All-Clips export.")
        return False
    if not all_actions:
        print("[ERROR]   No clip actions collected; nothing to export.")
        return False

    print(f"[INFO]   {len(all_actions)} take(s) ready -> {output_path}")
    print(f"[INFO]   Priming {len(all_actions)} action(s) (one throwaway export each); this may take a minute...")
    try:
        export_bundle(canonical_armature, all_actions, output_path, args.armature_only, prime=True)
        print(f"[SUMMARY]   All-Clips bundle written: {output_path}")
        return True
    except Exception as e:
        print(f"[FAIL]   All-Clips export failed: {e}")
        traceback.print_exc()
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    inputs = [os.path.abspath(p) for p in args.input]
    for p in inputs:
        if not os.path.isfile(p):
            print(f"[ERROR] Input not found: {p}")
            sys.exit(1)
    if args.meta and len(inputs) > 1:
        print("[ERROR] --meta cannot be used with multiple --input files (auto-detect per source instead).")
        sys.exit(1)

    output_root = os.path.abspath(args.output)
    os.makedirs(output_root, exist_ok=True)

    do_per_source   = not args.all_combined_only
    do_all_combined = args.all_combined or args.all_combined_only

    grand_success = 0
    grand_total   = 0
    if do_per_source:
        for input_path in inputs:
            s, t = process_source(input_path, output_root, args)
            grand_success += s
            grand_total   += t

    all_combined_ok = True
    if do_all_combined:
        all_combined_path = os.path.join(output_root, "All-Clips.fbx")
        all_combined_ok = export_all_combined(inputs, all_combined_path, args)

    print("")
    print("=" * 60)
    if do_per_source and not args.combined_only:
        print(f"[FINAL] Per-clip files: {grand_success}/{grand_total} across {len(inputs)} source(s)")
    if do_per_source and (args.combined or args.combined_only):
        print(f"[FINAL] Per-source bundles written under {output_root}")
    if do_all_combined:
        print(f"[FINAL] All-Clips bundle: {'OK' if all_combined_ok else 'FAILED'}")
    print(f"[FINAL] Output root: {output_root}")
    print("=" * 60)

    sys.exit(0 if all_combined_ok else 2)


if __name__ == "__main__":
    main()
