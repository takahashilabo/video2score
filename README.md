# video2score

ピアノ演奏動画（またはYouTube URL）から **両手ピアノ譜（大譜表 PDF）** を自動生成するツール。

## パイプライン

```
YouTube URL ──[yt-dlp]──┬
                        │
動画ファイル ────────────┤
                        │
                        ├─[ffmpeg]──────────────── 音声抽出（WAV）
                        │
                        ├─[piano / basic-pitch]── 音声 → MIDI（全音符）
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
uv sync --extra piano

# pip を使う場合
pip install -e ".[piano]"
```

### MuseScore（推奨・オプション）

[MuseScore 4](https://musescore.org/) をインストールすると、より高品質な楽譜を生成できます。  
未インストールの場合は verovio + cairosvg による自動フォールバックが使われます。

### ピアノ専用モデル

デフォルトの変換モデルは `piano`（ピアノ専用・高精度）です。  
`--extra piano` / `.[piano]` で torch と piano-transcription-inference をインストールしてください。

汎用モデル（torch 不要）を使う場合は `--model basic-pitch` を指定します:

```bash
uv sync  # piano extra なしでもインストール可能
```

## 使い方

### 基本

```bash
# ローカル動画ファイル → performance.pdf
video2score performance.mp4

# YouTube URL を直接指定 → score.pdf
video2score https://www.youtube.com/watch?v=XXXX

# 出力先を指定
video2score performance.mp4 -o my_score.pdf
video2score https://youtu.be/XXXX -o my_score.pdf
```

### モデル選択

```bash
# ピアノ専用モデル（デフォルト・高精度・要 torch）
video2score performance.mp4 --model piano

# 汎用モデル（torch 不要・軽量）
video2score performance.mp4 --model basic-pitch
```

### 楽譜オプション

```bash
# クオンタイズ: 音符の開始タイミングをグリッドに揃える
#   0.25 = 16分音符グリッド
#   0.5  = 8分音符グリッド（おすすめ）
#   1.0  = 4分音符グリッド
video2score performance.mp4 --quantize 0.5

# 最大音符長: 長い伸ばし音符を短く切りタイを減らす（デフォルト 0.5 拍 = 8分音符）
video2score performance.mp4 --max-beats 0.25   # 16分音符まで
video2score performance.mp4 --max-beats 1.0    # 4分音符まで（タイが増える）
```

### 音声変換オプション（basic-pitch モデル使用時）

```bash
# オンセット検出しきい値（デフォルト 0.5 / 高いほど音符が減る）
video2score performance.mp4 --model basic-pitch --onset-threshold 0.7

# フレーム検出しきい値（デフォルト 0.3）
video2score performance.mp4 --model basic-pitch --frame-threshold 0.5

# 最短音符長 ms（デフォルト 58.0 ms）
video2score performance.mp4 --model basic-pitch --min-note-length 80
```

### 中間ファイルの制御

```bash
# 中間 MIDI ファイルを出力先と同じフォルダに保持
video2score performance.mp4 --keep-midi

# 中間 MIDI の保存先ディレクトリを指定（--keep-midi も有効になる）
video2score performance.mp4 --midi-dir ./midi_out

# 音声変換をスキップして既存 MIDI を使用
video2score performance.mp4 --skip-transcription raw.mid

# 手分離をスキップして既存の 2トラック MIDI を使用
video2score performance.mp4 --skip-split piano.mid
```

### 手分離オプション

```bash
# 動画と MIDI のタイミングオフセットを手動指定（秒・省略時は自動推定）
video2score performance.mp4 --offset 0.5
```

### 典型的な組み合わせ例

```bash
# YouTube 動画を高精度モデル + 8分音符クオンタイズで変換
video2score https://youtu.be/XXXX --quantize 0.5

# ローカル動画をデバッグ用に中間ファイルを残しながら変換
video2score performance.mp4 --keep-midi --quantize 0.5

# basic-pitch で感度を上げて変換
video2score performance.mp4 --model basic-pitch --onset-threshold 0.3 --frame-threshold 0.2
```

### 各モジュールの単独実行

```bash
# 音声変換のみ
python -m video2score.audio2midi performance.mp4 -o raw.mid

# 手分離のみ
python -m video2score.split_hands performance.mp4 raw.mid --output-dir ./midi_out

# 楽譜生成のみ
python -m video2score.midi2score --input piano.mid --out score.pdf --quantize 0.5
```

## オプション一覧

| オプション | デフォルト | 説明 |
|---|---|---|
| `--model` | `piano` | 変換モデル: `piano` / `basic-pitch` |
| `--quantize` | `0.0`（無効） | クオンタイズグリッド（拍）: 0.25 / 0.5 / 1.0 |
| `--max-beats` | `0.5` | 最大音符長（拍）。小さいほどタイが減る |
| `--onset-threshold` | `0.5` | [basic-pitch] オンセット検出しきい値 |
| `--frame-threshold` | `0.3` | [basic-pitch] フレーム検出しきい値 |
| `--min-note-length` | `58.0` | [basic-pitch] 最短音符長（ms） |
| `--offset` | 自動推定 | 動画–MIDI タイミングオフセット（秒） |
| `--keep-midi` | — | 中間 MIDI を出力先フォルダに保持 |
| `--midi-dir DIR` | — | 中間 MIDI の出力先ディレクトリ |
| `--skip-transcription MIDI` | — | 音声変換をスキップして既存 MIDI を使用 |
| `--skip-split MIDI` | — | 手分離をスキップして既存 2トラック MIDI を使用 |
| `-o / --output` | 動画名.pdf | 出力 PDF パス |

## 動作要件

- Python 3.11+
- ffmpeg（システムにインストール済みであること）
- MuseScore 4（任意・なくても動作）
- yt-dlp（YouTube URL を使う場合、`pip install yt-dlp` で追加）

## 元プロジェクト

- [movie2midi](https://github.com/takahashilabo/movie2midi) — 動画→MIDI 変換
- [piano_movie2midi](https://github.com/takahashilabo/piano_movie2midi) — 手分離・楽譜生成
