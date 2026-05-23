"""
video2score: ピアノ演奏動画 → 両手ピアノ譜（大譜表 PDF）

使い方:
    video2score performance.mp4                  # 基本実行
    video2score performance.mp4 -o score.pdf     # 出力先指定
    video2score performance.mp4 --model piano    # 高精度モデル使用
    video2score performance.mp4 --keep-midi      # 中間 MIDI ファイルを保持

パイプライン:
    動画 → [ffmpeg] → 音声
         → [basic-pitch / piano-transcription] → piano.mid
         → [MediaPipe] → 左右手分離 → left.mid / right.mid / piano.mid
         → [MuseScore / verovio] → score.pdf
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="video2score",
        description="ピアノ演奏動画から両手ピアノ譜（PDF）を生成する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video", help="入力動画ファイル（例: performance.mp4）")
    parser.add_argument("-o", "--output", default=None,
                        help="出力 PDF パス（省略時: 動画と同名の .pdf）")

    # --- 音声変換オプション ---
    g_trans = parser.add_argument_group("音声→MIDI 変換オプション")
    g_trans.add_argument(
        "--model", choices=["basic-pitch", "piano"], default="basic-pitch",
        help="変換モデル: basic-pitch（汎用）/ piano（ピアノ専用・要 torch）",
    )
    g_trans.add_argument("--onset-threshold", type=float, default=0.5,
                         metavar="THR", help="[basic-pitch] オンセット検出しきい値")
    g_trans.add_argument("--frame-threshold", type=float, default=0.3,
                         metavar="THR", help="[basic-pitch] フレーム検出しきい値")
    g_trans.add_argument("--min-note-length", type=float, default=58.0,
                         metavar="MS", help="[basic-pitch] 最短音符長（ms）")

    # --- 手分離オプション ---
    g_split = parser.add_argument_group("手分離オプション")
    g_split.add_argument("--offset", type=float, default=None,
                         metavar="SEC",
                         help="動画-MIDI タイミングオフセット（秒）。省略時は自動推定")

    # --- 楽譜生成オプション ---
    g_score = parser.add_argument_group("楽譜生成オプション")
    g_score.add_argument("--max-beats", type=float, default=1.0,
                         metavar="BEATS",
                         help="楽譜上の最大音符長（拍）。0.5=8分音符まで")

    # --- 中間ファイル制御 ---
    g_mid = parser.add_argument_group("中間ファイル")
    g_mid.add_argument("--keep-midi", action="store_true",
                       help="中間 MIDI ファイルを出力先と同じフォルダに保持する")
    g_mid.add_argument("--midi-dir", default=None, metavar="DIR",
                       help="中間 MIDI の出力先ディレクトリ（指定すると --keep-midi も有効）")
    g_mid.add_argument("--skip-transcription", default=None, metavar="MIDI",
                       help="音声変換をスキップして既存 MIDI を使用する")
    g_mid.add_argument("--skip-split", default=None, metavar="MIDI",
                       help="手分離をスキップして既存の 2トラック MIDI を使用する")

    args = parser.parse_args()

    video_path = Path(args.video)
    if not video_path.exists():
        print(f"エラー: 動画ファイルが見つかりません: {video_path}", file=sys.stderr)
        raise SystemExit(1)

    output_path = Path(args.output) if args.output else video_path.with_suffix(".pdf")

    # 作業ディレクトリの決定
    if args.midi_dir:
        work_dir = Path(args.midi_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        use_tmpdir = False
    elif args.keep_midi:
        work_dir = output_path.parent
        use_tmpdir = False
    else:
        _tmp = tempfile.TemporaryDirectory()
        work_dir = Path(_tmp.name)
        use_tmpdir = True

    try:
        _run_pipeline(args, video_path, output_path, work_dir)
        print(f"\n完了: 楽譜を保存しました → {output_path}")
    finally:
        if use_tmpdir:
            _tmp.cleanup()


def _run_pipeline(args, video_path: Path, output_path: Path, work_dir: Path) -> None:
    from video2score.audio2midi import extract_audio, transcribe_basic_pitch, transcribe_piano
    from video2score.split_hands import split_hands
    from video2score.midi2score import fix_and_export

    # ── STEP 1 & 2: 動画 → MIDI ──────────────────────────────────────────
    if args.skip_split:
        piano_midi = Path(args.skip_split)
        if not piano_midi.exists():
            print(f"エラー: MIDI ファイルが見つかりません: {piano_midi}", file=sys.stderr)
            raise SystemExit(1)
        print(f"[手分離スキップ] {piano_midi} を使用")

    elif args.skip_transcription:
        raw_midi = Path(args.skip_transcription)
        if not raw_midi.exists():
            print(f"エラー: MIDI ファイルが見つかりません: {raw_midi}", file=sys.stderr)
            raise SystemExit(1)
        print(f"[音声変換スキップ] 既存 MIDI: {raw_midi}")
        print("\n[Step 2/3] 手分離中...")
        split_hands(str(video_path), str(raw_midi), str(work_dir), args.offset)
        piano_midi = work_dir / "piano.mid"

    else:
        print(f"[Step 1/3] 音声抽出中: {video_path.name}")
        audio_path = work_dir / "audio.wav"
        extract_audio(str(video_path), str(audio_path))

        print(f"\n[Step 2a/3] MIDI 変換中 (model={args.model})...")
        raw_midi = work_dir / "raw.mid"
        if args.model == "basic-pitch":
            transcribe_basic_pitch(
                str(audio_path), str(raw_midi),
                onset_threshold=args.onset_threshold,
                frame_threshold=args.frame_threshold,
                min_note_length=args.min_note_length,
            )
        else:
            transcribe_piano(str(audio_path), str(raw_midi))
        print(f"  MIDI 出力: {raw_midi}")

        print("\n[Step 2b/3] 手分離中...")
        split_hands(str(video_path), str(raw_midi), str(work_dir), args.offset)
        piano_midi = work_dir / "piano.mid"

    # ── STEP 3: MIDI → PDF ────────────────────────────────────────────────
    print("\n[Step 3/3] 楽譜生成中...")
    fix_and_export(str(piano_midi), str(output_path), max_beats=args.max_beats)
