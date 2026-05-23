"""
動画フレームの手検出により MIDI ノートを左右手に分離するモジュール。

piano_movie2midi プロジェクトの実装を元に、video2score パイプラインに統合。

アルゴリズム:
  1. MIDI からノートを読み込み（タイミングを秒で保持）
  2. 動画音声と MIDI のオンセット相関でタイミングオフセットを推定
  3. 動画フレームから鍵盤領域を検出
  4. MediaPipe Hands でノートオンセット付近のフレームを解析
  5. 鍵盤上の手の中心位置を基準にノートを左右に割り当て
  6. left.mid / right.mid / piano.mid (2トラック) を出力
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import cv2
import librosa
import mediapipe as mp
import mido
import numpy as np
from scipy.signal import correlate


# ---------------------------------------------------------------------------
# MIDI 読み込み
# ---------------------------------------------------------------------------

def _load_notes(midi_path: str) -> tuple[list[dict], float]:
    """MIDI ファイルからノートイベントを秒単位で読み込む。

    Returns:
        notes: ノートリスト (start/end 秒, note, velocity, channel)
        ticks_per_beat: テンポ参照用
    """
    mid = mido.MidiFile(midi_path)
    tempo = 500000  # 120 BPM デフォルト
    notes: list[dict] = []

    for track in mid.tracks:
        abs_tick = 0
        abs_sec = 0.0
        active: dict[tuple, tuple] = {}

        for msg in track:
            dt_sec = mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
            abs_tick += msg.time
            abs_sec += dt_sec

            if msg.type == "set_tempo":
                tempo = msg.tempo
            elif msg.type == "note_on" and msg.velocity > 0:
                active[(msg.channel, msg.note)] = (abs_tick, abs_sec, msg.velocity)
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if key in active:
                    s_tick, s_sec, vel = active.pop(key)
                    notes.append({
                        "note": msg.note,
                        "start": s_sec,
                        "end": abs_sec,
                        "start_tick": s_tick,
                        "end_tick": abs_tick,
                        "velocity": vel,
                        "channel": msg.channel,
                        "hand": None,
                    })

    notes.sort(key=lambda n: n["start"])
    return notes, mid.ticks_per_beat


# ---------------------------------------------------------------------------
# 動画と MIDI のタイミング同期
# ---------------------------------------------------------------------------

def _estimate_offset(video_path: str, notes: list[dict], sr: int = 22050, hop: int = 512) -> float:
    """動画音声と MIDI オンセットの相互相関でタイミングオフセットを推定する。

    Returns:
        offset (秒): video_time = midi_time + offset
    """
    y, _ = librosa.load(str(video_path), sr=sr, mono=True)
    audio_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)

    n_frames = len(audio_env)
    midi_env = np.zeros(n_frames)
    for note in notes:
        idx = int(note["start"] * sr / hop)
        if 0 <= idx < n_frames:
            midi_env[idx] += note["velocity"] / 127.0

    corr = correlate(audio_env, midi_env, mode="full")
    lag = int(corr.argmax()) - (len(midi_env) - 1)
    return -(lag * hop / sr)


# ---------------------------------------------------------------------------
# 鍵盤領域検出
# ---------------------------------------------------------------------------

def _detect_keyboard(cap: cv2.VideoCapture) -> dict:
    """平均フレームから鍵盤の行と左右端ピクセルを推定する。"""
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    gray_frames = []
    for idx in range(0, min(total, 300), max(1, min(total, 300) // 8)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if ret:
            gray_frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))

    if not gray_frames:
        return {"left": 0, "right": w - 1, "row": h * 3 // 4}

    avg = np.mean(gray_frames, axis=0).astype(np.uint8)

    # 横方向エッジ量が最大の行を鍵盤行として選ぶ（鍵の境界線が多い）
    sobelx = np.abs(cv2.Sobel(avg, cv2.CV_64F, 1, 0, ksize=3))
    row_score = sobelx.sum(axis=1)

    # 下半分を優先（鍵盤は画面下部が多い）
    search_from = h // 3
    kb_row = search_from + int(row_score[search_from:].argmax())

    # 鍵盤行の輝度で左右端を決定
    line = avg[kb_row, :].astype(float)
    threshold = line.max() * 0.55
    bright = line > threshold

    if bright.sum() < w * 0.2:
        return {"left": 0, "right": w - 1, "row": kb_row}

    left = int(np.argmax(bright))
    right = int(len(bright) - np.argmax(bright[::-1]) - 1)
    return {"left": left, "right": right, "row": kb_row}


def _note_to_x(note_num: int, keyboard: dict) -> float:
    """MIDI ノート番号を鍵盤上のピクセル X 座標に変換する（線形近似）。"""
    ratio = (max(21, min(108, note_num)) - 21) / (108 - 21)
    return keyboard["left"] + ratio * (keyboard["right"] - keyboard["left"])


# ---------------------------------------------------------------------------
# MediaPipe 手検出
# ---------------------------------------------------------------------------

def _detect_hands(frame: np.ndarray, detector) -> Optional[tuple[float, float]]:
    """フレーム中の両手の X 座標 (left_x, right_x) を返す。片手のみの場合 None。"""
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    result = detector.process(rgb)

    if not result.multi_hand_landmarks or len(result.multi_hand_landmarks) < 2:
        return None

    h, w = frame.shape[:2]
    xs = []
    for lm in result.multi_hand_landmarks:
        # 手首 (landmark 0) の X 座標
        xs.append(lm.landmark[0].x * w)

    xs.sort()
    return xs[0], xs[1]  # (left_x, right_x)


# ---------------------------------------------------------------------------
# MIDI 出力
# ---------------------------------------------------------------------------

def _write_midi_files(notes: list[dict], output_dir: Path, tpb: int = 960, tempo: int = 500000) -> None:
    """left.mid / right.mid / piano.mid を出力する。

    ノートのタイミングは秒から固定テンポ (120 BPM) + 960 tpb に変換する。
    """
    beats_per_sec = 1_000_000 / tempo

    def to_ticks(sec: float) -> int:
        return max(0, int(sec * beats_per_sec * tpb))

    def build_track(hand_notes: list[dict], channel: int, track_name: str = "") -> mido.MidiTrack:
        track = mido.MidiTrack()
        if track_name:
            track.append(mido.MetaMessage("track_name", name=track_name, time=0))
        track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
        track.append(mido.Message("program_change", channel=channel, program=0, time=0))

        events: list[tuple] = []
        for n in hand_notes:
            st = to_ticks(n["start"])
            et = to_ticks(n["end"])
            if et <= st:
                et = st + tpb // 8  # 最短32分音符
            events.append((st, 1, n["note"], n["velocity"], channel))  # 1=on
            events.append((et, 0, n["note"], 0, channel))              # 0=off

        # 同一 tick では note_off → note_on の順（MIDI 慣例）
        events.sort(key=lambda e: (e[0], e[1]))

        cur = 0
        active: set = set()
        for tick, kind, note_num, vel, ch in events:
            delta = tick - cur
            if kind == 1:  # note_on
                active.add(note_num)
                track.append(mido.Message("note_on", note=note_num, velocity=vel, channel=ch, time=delta))
                cur = tick
            else:  # note_off
                if note_num in active:
                    active.discard(note_num)
                    track.append(mido.Message("note_off", note=note_num, velocity=0, channel=ch, time=delta))
                    cur = tick

        track.append(mido.MetaMessage("end_of_track", time=0))
        return track

    for hand in ("left", "right"):
        hand_notes = [n for n in notes if n.get("hand") == hand]
        mid = mido.MidiFile(ticks_per_beat=tpb)
        mid.tracks.append(build_track(hand_notes, channel=0))
        mid.save(str(output_dir / f"{hand}.mid"))

    # piano.mid: 右手 ch0 + 左手 ch1（MuseScore が 2 段譜として読む）
    piano = mido.MidiFile(ticks_per_beat=tpb)
    piano.tracks.append(build_track(
        [n for n in notes if n.get("hand") == "right"], channel=0, track_name="Right Hand"
    ))
    piano.tracks.append(build_track(
        [n for n in notes if n.get("hand") == "left"], channel=1, track_name="Left Hand"
    ))
    piano.save(str(output_dir / "piano.mid"))


# ---------------------------------------------------------------------------
# メイン処理
# ---------------------------------------------------------------------------

def split_hands(
    video_path: str,
    midi_path: str,
    output_dir: str = ".",
    manual_offset: Optional[float] = None,
) -> None:
    """MIDI ノートを動画解析で左右手に分離して出力する。

    Args:
        video_path:    ピアノ演奏動画ファイル
        midi_path:     入力 MIDI ファイル（audio2midi の出力）
        output_dir:    出力先ディレクトリ
        manual_offset: 動画-MIDI タイミングオフセット（秒）。None なら自動推定
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("  MIDI 読み込み中...")
    notes, _ = _load_notes(midi_path)
    if not notes:
        raise ValueError("MIDI ファイルにノートが見つかりません")

    # タイミングオフセット
    if manual_offset is not None:
        offset = manual_offset
        print(f"  タイミングオフセット: {offset:.3f}s (手動指定)")
    else:
        print("  動画-MIDI タイミングオフセット推定中...")
        try:
            offset = _estimate_offset(video_path, notes)
            print(f"  タイミングオフセット: {offset:.3f}s")
        except Exception as exc:
            print(f"  オフセット推定失敗 ({exc})、0.0 を使用")
            offset = 0.0

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"動画を開けません: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    # 鍵盤検出
    print("  鍵盤領域検出中...")
    keyboard = _detect_keyboard(cap)
    print(f"  鍵盤: x=[{keyboard['left']}, {keyboard['right']}], 行={keyboard['row']}")

    # 必要なフレームインデックスを収集
    WINDOW = 7
    needed: set[int] = set()
    for note in notes:
        center = max(0, int((note["start"] + offset) * fps))
        for d in range(-(WINDOW // 2), WINDOW // 2 + 1):
            fi = center + d
            if fi >= 0:
                needed.add(fi)

    # 手検出（連続フレームは seek せず順次読み込み）
    print(f"  手検出中 ({len(needed)} フレーム)...")
    mp_hands = mp.solutions.hands
    hand_at: dict[int, Optional[tuple]] = {}

    with mp_hands.Hands(
        static_image_mode=True,
        max_num_hands=2,
        min_detection_confidence=0.5,
    ) as detector:
        expected_next = -1
        for fi in sorted(needed):
            if fi != expected_next:
                cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
            ret, frame = cap.read()
            if ret:
                hand_at[fi] = _detect_hands(frame, detector)
            expected_next = fi + 1

    cap.release()

    # ノートに左右手を割り当て
    print("  ノートを左右手に割り当て中...")
    current_left: Optional[float] = None
    current_right: Optional[float] = None

    # オンセットに近い順（0, ±1, ±2, ...）で探索
    search_deltas = [0] + [d for i in range(1, WINDOW // 2 + 1) for d in (i, -i)]

    for note in notes:
        center = max(0, int((note["start"] + offset) * fps))

        for d in search_deltas:
            fi = center + d
            if fi in hand_at and hand_at[fi] is not None:
                current_left, current_right = hand_at[fi]
                break

        note_x = _note_to_x(note["note"], keyboard)

        if current_left is not None and current_right is not None:
            mid_x = (current_left + current_right) / 2.0
            note["hand"] = "left" if note_x < mid_x else "right"
        else:
            # フォールバック: ピッチで判定（中央 C = 60 を境界）
            note["hand"] = "left" if note["note"] < 60 else "right"

    left_count = sum(1 for n in notes if n["hand"] == "left")
    right_count = sum(1 for n in notes if n["hand"] == "right")
    print(f"  ノート割り当て: 左手 {left_count} / 右手 {right_count}")

    _write_midi_files(notes, output_dir)
    print(f"  MIDI ファイル出力: {output_dir}/{{left,right,piano}}.mid")


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="動画の手検出で MIDI ノートを左右手に分離する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("video", help="入力動画ファイル")
    parser.add_argument("midi", help="入力 MIDI ファイル")
    parser.add_argument("--output-dir", default=".", help="出力ディレクトリ")
    parser.add_argument("--offset", type=float, default=None,
                        help="動画-MIDI タイミングオフセット（秒）")
    args = parser.parse_args()

    for path, label in [(args.video, "動画"), (args.midi, "MIDI")]:
        if not Path(path).exists():
            print(f"エラー: {label}ファイルが見つかりません: {path}", file=sys.stderr)
            raise SystemExit(1)

    split_hands(args.video, args.midi, args.output_dir, args.offset)


if __name__ == "__main__":
    main()
