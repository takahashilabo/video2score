# video2score

ピアノ演奏動画から **両手ピアノ譜（大譜表 PDF）** を自動生成するツール。

## パイプライン

```
動画ファイル
  │
  ├─[ffmpeg]──────────────── 音声抽出（WAV）
  │
  ├─[basic-pitch / piano]── 音声 → MIDI（全音符）
  │
  ├─[MediaPipe Hands]──────  動画フレームで手の位置を検出
  │                          鍵盤上の X 座標で左右手に分離
  │                          → left.mid / right.mid / piano.mid
  │
  └─[MuseScore / verovio]── 2トラック MIDI → PDF 楽譜
                             ト音記号（右手）＋ヘ音記号（左手）
```

## インストール

```bash
# uv を使う場合（推奨）
uv sync

# pip を使う場合
pip install -e .
```

### MuseScore（推奨・オプション）

[MuseScore 4](https://musescore.org/) をインストールすると、より高品質な楽譜を生成できます。  
未インストールの場合は verovio + cairosvg による自動フォールバックが使われます。

### ピアノ専用モデル（オプション）

`--model piano` を使うには torch が必要です:

```bash
uv sync --extra piano
# または
pip install -e ".[piano]"
```

## 使い方

### 基本

```bash
video2score performance.mp4
# → performance.pdf が生成される
```

### オプション

```bash
# 出力先指定
video2score performance.mp4 -o my_score.pdf

# ピアノ専用高精度モデル（要 torch）
video2score performance.mp4 --model piano

# 中間 MIDI ファイルを保持
video2score performance.mp4 --keep-midi

# 手動でタイミングオフセット指定（秒）
video2score performance.mp4 --offset 0.5

# 楽譜の最大音符長を 8 分音符に変更
video2score performance.mp4 --max-beats 0.5

# 音声変換済みの MIDI がある場合はスキップ
video2score performance.mp4 --skip-transcription raw.mid

# 手分離済みの 2トラック MIDI がある場合はスキップ
video2score performance.mp4 --skip-split piano.mid
```

### 各モジュールの単独実行

```bash
# 音声変換のみ
python -m video2score.audio2midi performance.mp4 -o raw.mid

# 手分離のみ
python -m video2score.split_hands performance.mp4 raw.mid --output-dir ./midi_out

# 楽譜生成のみ
python -m video2score.midi2score --input piano.mid --out score.pdf
```

## 動作要件

- Python 3.11+
- ffmpeg（システムにインストール済みであること）
- MuseScore 4（任意・なくても動作）

## 元プロジェクト

- [movie2midi](https://github.com/takahashilabo/movie2midi) — 動画→MIDI 変換
- [piano_movie2midi](https://github.com/takahashilabo/piano_movie2midi) — 手分離・楽譜生成
