import zlib, struct

def decode_png(path):
    with open(path, 'rb') as f: data = f.read()
    pos = 8; idat = bytearray()
    while pos < len(data):
        length, ctype = struct.unpack('>I4s', data[pos:pos+8])
        pos += 8; cdata = data[pos:pos+length]; pos += length + 4
        if ctype == b'IHDR': w, h, depth, colortype, comp, filt, inter = struct.unpack('>IIBBBBB', cdata)
        elif ctype == b'IDAT': idat.extend(cdata)
        elif ctype == b'IEND': break
    raw = zlib.decompress(idat)
    bpp = 3
    stride = w * bpp; pixels = bytearray(w * h * bpp); raw_pos = 0
    for y in range(h):
        filter_type = raw[raw_pos]; raw_pos += 1
        line = bytearray(raw[raw_pos : raw_pos + stride]); raw_pos += stride
        prev_line_offset = (y - 1) * stride if y > 0 else None
        curr_line_offset = y * stride
        if filter_type == 0: pass
        elif filter_type == 1:
            for x in range(bpp, stride): line[x] = (line[x] + line[x - bpp]) & 0xff
        elif filter_type == 2:
            if prev_line_offset is not None:
                for x in range(stride): line[x] = (line[x] + pixels[prev_line_offset + x]) & 0xff
        elif filter_type == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                up = pixels[prev_line_offset + x] if prev_line_offset is not None else 0
                line[x] = (line[x] + ((left + up) >> 1)) & 0xff
        elif filter_type == 4:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0
                b = pixels[prev_line_offset + x] if prev_line_offset is not None else 0
                c = pixels[prev_line_offset + x - bpp] if (prev_line_offset is not None and x >= bpp) else 0
                p = a + b - c; pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 0xff
        pixels[curr_line_offset : curr_line_offset + stride] = line
    return w, h, pixels

w, h, pixels = decode_png('/root/aqua/real_CAD.png')

def get_rgb(x, y):
    idx = (y * w + x) * 3
    return pixels[idx], pixels[idx+1], pixels[idx+2]

# Scale:
# 700 mm = 533 px -> 0.7614 px/mm (1 mm = 0.7614 px)
# 600 mm = 451 px -> 0.7517 px/mm (1 mm = 0.7517 px)
# Average: ~0.7565 px/mm (1 px = 1.3218 mm)
PX_PER_MM_X = 533.0 / 700.0
PX_PER_MM_Y = 451.0 / 600.0

print(f"PX_PER_MM_X: {PX_PER_MM_X:.4f}, PX_PER_MM_Y: {PX_PER_MM_Y:.4f}")

# Tank walls & floor:
# Left wall x=56, right wall x=589 (width 533 px = 700 mm)
# Rim y=95, floor y=546 (height 451 px = 600 mm)
# Coordinate system relative to inside floor (Y=0 at y=546) and inside left wall (X=0 at x=56):
def to_mm(px, py):
    x_mm = (px - 56) / PX_PER_MM_X
    y_mm = (546 - py) / PX_PER_MM_Y
    return round(x_mm, 1), round(y_mm, 1)

# 1. Impeller & Shaft:
# Find shaft:
# The shaft is reddish/brown cylinder between motor bottom and impeller collar
# Let's inspect center line x=313:
print("--- SHAFT & IMPELLER INSPECTION ALONG X=313 ---")
for y in range(200, 340):
    r, g, b = get_rgb(313, y)
    if r > 100 and r > g * 1.3: # reddish
        print(f"Reddish at Y={y}: rgb=({r},{g},{b}) -> mm={to_mm(313, y)}")

