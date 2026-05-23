""" 
2トラック piano MIDI → PDF 楽譜 変換モジュール。

piano_movie2midi の fix_mscz.py を元に、クロスプラットフォーム対応と
verovio/music21 フォールバックを追加。

優先順位:
  1. MuseScore 4/3 (インストール済みの場合) — 最高品質
  2. music21 + verovio + cairosvg — MuseScore 不要のフォールバック
"""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import mido


# ---------------------------------------------------------------------------
# MuseScore 実行ファイルの検索
# ---------------------------------------------------------------------------

def _find_musescore() -> str | None:
    """OS 別に MuseScore 実行ファイルを探してパスを返す。見つからなければ None。"""
    system = platform.system()

    if system == "Darwin":
        candidates = [
            "/Applications/MuseScore 4.app/Contents/MacOS/mscore",
            "/Applications/MuseScore 3.app/Contents/MacOS/mscore",
        ]
    elif system == "Linux":
        candidates = ["mscore4portable", "mscore4", "musescore4", "mscore", "musescore"]
    elif system == "Windows":
        candidates = [
            r"C:\Program Files\MuseScore 4\bin\MuseScore4.exe",
            r"C:\Program Files (x86)\MuseScore 4\bin\MuseScore4.exe",
            r"C:\Program Files\MuseScore 3\bin\MuseScore3.exe",
        ]
    else:
        candidates = ["mscore", "musescore"]

    for path in candidates:
        if Path(path).is_file():
            return path
        found = shutil.which(path)
        if found:
            return found

    return None


# ---------------------------------------------------------------------------
# MIDI 前処理
# ---------------------------------------------------------------------------

def _shorten_notes(midi_path: str, output_path: str, max_beats: float = 0.5) -> None:
    """音符の長さを max_beats 拍に制限する（長い伸ばし記号を抑制）。"""
    mid = mido.MidiFile(midi_path)
    tpb = mid.ticks_per_beat
    max_ticks = int(max_beats * tpb)
    out = mido.MidiFile(ticks_per_beat=tpb)

    for track in mid.tracks:
        # 絶対 tick に変換
        events: list[list] = []
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            events.append([abs_tick, msg.copy(time=0)])

        # 長すぎるノートに早期 note_off を挿入
        note_on_at: dict[tuple, int] = {}
        extra_offs: list[list] = []

        for abs_t, msg in events:
            if msg.type == "note_on" and msg.velocity > 0:
                note_on_at[(msg.channel, msg.note)] = abs_t
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if key in note_on_at:
                    duration = abs_t - note_on_at.pop(key)
                    if duration > max_ticks:
                        early = abs_t - duration + max_ticks
                        extra_offs.append([
                            early,
                            mido.Message("note_off", channel=msg.channel,
                                         note=msg.note, velocity=0, time=0),
                        ])

        all_events = events + extra_offs
        # note_off を note_on より先にソート（同一 tick の場合）
        all_events.sort(key=lambda e: (e[0], 1 if (e[1].type == "note_on" and e[1].velocity > 0) else 0))

        out_track = mido.MidiTrack()
        out.tracks.append(out_track)

        active: set = set()
        cur = 0
        for abs_t, msg in all_events:
            delta = abs_t - cur
            key = (getattr(msg, "channel", None), getattr(msg, "note", None))

            if msg.type == "note_on" and msg.velocity > 0:
                active.add(key)
                out_track.append(msg.copy(time=delta))
                cur = abs_t
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                if key in active:
                    active.discard(key)
                    out_track.append(msg.copy(time=delta))
                    cur = abs_t
            else:
                out_track.append(msg.copy(time=delta))
                cur = abs_t

    out.save(output_path)


def _quantize_midi(midi_path: str, output_path: str, grid_beats: float = 0.25) -> None:
    """ノートのオンセット時刻を grid_beats 拍グリッドにスナップする。

    grid_beats の目安:
      0.125 = 32分音符グリッド
      0.25  = 16分音符グリッド（デフォルト）
      0.5   = 8分音符グリッド
      1.0   = 4分音符グリッド
    """
    mid = mido.MidiFile(midi_path)
    tpb = mid.ticks_per_beat
    grid = max(1, round(grid_beats * tpb))
    out = mido.MidiFile(ticks_per_beat=tpb)

    for track in mid.tracks:
        events: list[list] = []
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time
            events.append([abs_tick, msg.copy(time=0)])

        # list を使って同じ音程の反復音を FIFO で正しく対応する
        note_orig_on: dict[tuple, list[int]] = {}
        note_snapped_on: dict[tuple, list[int]] = {}
        new_events: list[list] = []

        for abs_t, msg in events:
            if msg.type == "note_on" and msg.velocity > 0:
                key = (msg.channel, msg.note)
                snapped = round(abs_t / grid) * grid
                note_orig_on.setdefault(key, []).append(abs_t)
                note_snapped_on.setdefault(key, []).append(snapped)
                new_events.append([snapped, msg])
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if note_orig_on.get(key):
                    orig_on = note_orig_on[key].pop(0)
                    snapped_on = note_snapped_on[key].pop(0)
                    new_events.append([snapped_on + (abs_t - orig_on), msg])
                else:
                    new_events.append([abs_t, msg])
            else:
                new_events.append([abs_t, msg])

        new_events.sort(key=lambda e: (e[0], 1 if (e[1].type == "note_on" and e[1].velocity > 0) else 0))

        out_track = mido.MidiTrack()
        out.tracks.append(out_track)
        cur = 0
        for abs_t, msg in new_events:
            out_track.append(msg.copy(time=max(0, abs_t - cur)))
            cur = abs_t

    out.save(output_path)


def _prepare_midi(midi_path: str, output_path: str, max_beats: float = 0.5,
                  quantize: float = 0.0) -> None:
    """音符を短縮し、MuseScore が適切なト音/ヘ音記号を割り当てるよう
    右手 = Violin (program 40)、左手 = Cello (program 42) を設定する。

    MuseScore インポート後に fix_mscx() でラベルを「ピアノ 右手/左手」に書き換える。
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        src = midi_path

        if quantize > 0:
            quantized = str(Path(tmpdir) / "quantized.mid")
            _quantize_midi(src, quantized, quantize)
            src = quantized

        shortened = str(Path(tmpdir) / "short.mid")
        _shorten_notes(src, shortened, max_beats)

        mid = mido.MidiFile(shortened)
        programs = [40, 42]  # Violin, Cello

        for i, track in enumerate(mid.tracks):
            ch = min(i, len(programs) - 1)
            prog = programs[ch]

            # 既存の program_change を置換、なければ先頭に挿入
            has_pc = any(m.type == "program_change" for m in track)
            if has_pc:
                new_msgs = []
                for m in track:
                    if m.type == "program_change":
                        new_msgs.append(mido.Message("program_change",
                                                      channel=ch, program=prog, time=m.time))
                    else:
                        new_msgs.append(m)
                track[:] = new_msgs
            else:
                insert_at = next(
                    (j + 1 for j, m in enumerate(track) if m.type == "track_name"), 0
                )
                track.insert(insert_at, mido.Message("program_change",
                                                      channel=ch, program=prog, time=0))

        mid.save(output_path)


# ---------------------------------------------------------------------------
# MuseScore XML パッチ
# ---------------------------------------------------------------------------

def _fix_mscx(content: str) -> str:
    """MSCZ 内の MSCX XML からすべての楽器名ラベルを削除する。

    MuseScore のバージョンによって楽器名が異なるため（Violin/Treble Viol 等）、
    特定名を狙わずすべての longName / shortName を空にする。
    """
    content = re.sub(r"<longName>[^<]*</longName>", "<longName></longName>", content)
    content = re.sub(r"<shortName>[^<]*</shortName>", "<shortName></shortName>", content)
    return content


# ---------------------------------------------------------------------------
# MuseScore によるエクスポート
# ---------------------------------------------------------------------------

def _export_musescore(midi_path: str, output_path: str, mscore: str) -> bool:
    """MuseScore で MIDI → PDF エクスポート。成功すれば True。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_p = Path(tmpdir)

        # MIDI → MSCZ
        mscz = tmpdir_p / "score.mscz"
        res = subprocess.run(
            [mscore, "-o", str(mscz), str(midi_path)],
            capture_output=True, timeout=180,
        )
        if not mscz.exists():
            print(f"    MuseScore MIDI→MSCZ 失敗: {res.stderr.decode()[:200]}")
            return False

        # MSCZ (zip) 内の MSCX を修正
        patched = tmpdir_p / "patched.mscz"
        try:
            with zipfile.ZipFile(mscz, "r") as zin, \
                 zipfile.ZipFile(patched, "w", zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename.endswith(".mscx"):
                        data = _fix_mscx(data.decode("utf-8")).encode("utf-8")
                    zout.writestr(item, data)
        except zipfile.BadZipFile:
            # MuseScore 4 の一部バージョンは平ファイル形式の場合あり
            patched = mscz

        # MSCZ → PDF
        res = subprocess.run(
            [mscore, "-o", str(output_path), str(patched)],
            capture_output=True, timeout=180,
        )
        if not Path(output_path).exists():
            print(f"    MuseScore MSCZ→PDF 失敗: {res.stderr.decode()[:200]}")
            return False

        return True


# ---------------------------------------------------------------------------
# verovio / music21 フォールバック
# ---------------------------------------------------------------------------

def _export_verovio(midi_path: str, output_path: str) -> bool:
    """music21 + verovio + cairosvg で PDF を生成する（MuseScore 不要）。"""
    try:
        import music21
        import verovio
        import cairosvg
    except ImportError as exc:
        print(f"    verovio フォールバック利用不可: {exc}")
        return False

    try:
        score = music21.converter.parse(midi_path)
        for part in score.parts:
            part.partName = ""
            part.partAbbreviation = ""

        with tempfile.TemporaryDirectory() as tmpdir:
            xml_path = str(Path(tmpdir) / "score.xml")
            score.write("musicxml", fp=xml_path)

            with open(xml_path, encoding="utf-8") as f:
                xml_str = f.read()

            tk = verovio.toolkit()
            # 全ページを縦に連結した長い SVG として出力
            try:
                import json as _json
                tk.setOptions(_json.dumps({
                    "pageHeight": 60000,
                    "pageWidth": 2100,
                    "scale": 35,
                    "adjustPageHeight": True,
                    "footer": "none",
                    "header": "none",
                }))
            except Exception:
                pass

            tk.loadData(xml_str)
            svg = tk.renderToSVG(1)

            svg_path = str(output_path).replace(".pdf", ".svg")
            with open(svg_path, "w", encoding="utf-8") as f:
                f.write(svg)

            if str(output_path).endswith(".pdf"):
                cairosvg.svg2pdf(url=svg_path, write_to=str(output_path))
                print(f"    SVG 中間ファイル: {svg_path}")
            return True

    except Exception as exc:
        print(f"    verovio エクスポート失敗: {exc}")
        return False


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def fix_and_export(
    midi_path: str,
    output_path: str,
    max_beats: float = 0.5,
    quantize: float = 0.0,
) -> None:
    """piano.mid (2トラック) → PDF 楽譜のメインパイプライン。

    Args:
        midi_path:   入力 MIDI ファイル（右手 ch0 + 左手 ch1）
        output_path: 出力 PDF ファイルパス
        max_beats:   最大音符長（拍）。デフォルト 0.5 = 8分音符まで（タイを抑制）
        quantize:    クオンタイズグリッド（拍）。0 で無効。
                     例: 0.25 = 16分音符, 0.5 = 8分音符, 1.0 = 4分音符
    """
    midi_path = Path(midi_path)
    output_path = Path(output_path)

    if not midi_path.exists():
        raise FileNotFoundError(f"MIDI ファイルが見つかりません: {midi_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        prepared = str(Path(tmpdir) / "prepared.mid")
        if quantize > 0:
            print(f"  クオンタイズ中 (grid={quantize}拍)...")
        print("  音符長を整形中...")
        _prepare_midi(str(midi_path), prepared, max_beats, quantize)

        mscore = _find_musescore()
        if mscore:
            print(f"  MuseScore 使用: {mscore}")
            if _export_musescore(prepared, str(output_path), mscore):
                return
            print("  MuseScore 失敗、verovio にフォールバック")
        else:
            print("  MuseScore が見つかりません、verovio を使用")

        if not _export_verovio(prepared, str(output_path)):
            raise RuntimeError(
                "楽譜生成に失敗しました。\n"
                "MuseScore 4 をインストールするか、cairosvg / verovio が正しく\n"
                "インストールされているか確認してください。"
            )


def main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="ピアノ MIDI（2トラック）を PDF 楽譜に変換する",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--input", default="piano.mid", help="入力 MIDI ファイル")
    parser.add_argument("--out", default="piano.pdf", help="出力 PDF ファイル")
    parser.add_argument("--max-beats", type=float, default=0.5,
                        help="最大音符長（拍）。小さいほどタイが減りスッキリした譜面になる")
    parser.add_argument(
        "--quantize", type=float, default=0.0, metavar="BEATS",
        help="クオンタイズグリッド（拍）。0=無効, 0.25=16分音符, 0.5=8分音符, 1.0=4分音符",
    )
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"エラー: {args.input} が見つかりません。先に split_hands を実行してください。",
              file=sys.stderr)
        raise SystemExit(1)

    fix_and_export(args.input, args.out, max_beats=args.max_beats, quantize=args.quantize)
    print(f"完了: {args.out}")


if __name__ == "__main__":
    main()
