# -*- coding: utf-8 -*-
"""
生成 WorkBuddySwitcher 图标（双猫版）：
绿色原版猫 + 蓝色主题猫（全分辨率色相偏移），斜向并排，可选切换徽章。
输出 wbicon.ico（多尺寸）和 icon_preview.png（预览）。
"""
from PIL import Image, ImageDraw, ImageFilter
import colorsys

SRC = r"D:\Program Files\WorkBuddy\resources\app.asar.unpacked\resources\icon.png"
OUT_ICO = "wbicon.ico"
OUT_PNG = "icon_preview.png"

base = Image.open(SRC).convert("RGBA")

# ---- 全分辨率色相偏移：青绿 -> 蓝（白色不受影响，猫脸跟随主题变色）
def hue_shift(img: Image.Image, delta: float) -> Image.Image:
    img = img.convert("RGBA")
    channels = [img.getchannel(c) for c in ("R", "G", "B", "A")]
    px = img.load()
    out = img.copy()
    po = out.load()
    w, h = img.size
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            hh, ll, ss = colorsys.rgb_to_hls(r/255, g/255, b/255)
            if ss > 0.12 and ll > 0.04 and ll < 0.96:  # 只动彩色像素
                hh = (hh + delta) % 1.0
                r2, g2, b2 = colorsys.hls_to_rgb(hh, ll, ss)
                po[x, y] = (int(r2*255), int(g2*255), int(b2*255), a)
    return out

blue = hue_shift(base, 0.13)

# ---- 斜向并排合成
TILE = 640
green_t = base.resize((TILE, TILE), Image.LANCZOS)
blue_t = blue.resize((TILE, TILE), Image.LANCZOS)

canvas = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
# 阴影
sh = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
sd = ImageDraw.Draw(sh)
sd.rounded_rectangle([390, 410, 390+TILE, 410+TILE], radius=int(TILE*0.185), fill=(0, 0, 0, 90))
sd.rounded_rectangle([36, 66, 36+TILE, 66+TILE], radius=int(TILE*0.185), fill=(0, 0, 0, 60))
canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(22)))
canvas.alpha_composite(blue_t, (368, 388))   # 蓝猫（右下，后层）
canvas.alpha_composite(green_t, (16, 40))    # 绿猫（左上，前层）
canvas.save("icon_preview.png")
canvas.save(OUT_ICO, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("生成完成:", OUT_ICO, OUT_PNG)
