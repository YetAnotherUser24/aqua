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
    bpp = 3; stride = w * bpp; pixels = bytearray(w * h * bpp); raw_pos = 0
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

# Measure Sensor Probe (around X=140-165, Y=390-500)
# Look at crop probe_cad_crop.png
# Let's inspect rows Y=400 to Y=500:
probe_xs = []
probe_ys = []
for y in range(350, 500):
    for x in range(140, 175):
        r, g, b = get_rgb(x, y)
        # dark grey probe body (not pure white background, not aluminum rail)
        if 40 < r < 140 and abs(r-g) < 10 and abs(g-b) < 10:
            probe_xs.append(x)
            probe_ys.append(y)

print("=== SENSOR PROBE ===")
print(f"Sensor X span: min={min(probe_xs)}, max={max(probe_xs)}, width={max(probe_xs)-min(probe_xs)+1} px")
print(f"Sensor Y span: min={min(probe_ys)}, max={max(probe_ys)}, height={max(probe_ys)-min(probe_ys)+1} px")

# Measure Mixer T-200:
# Cylinder body: X ~ 380 to 520, Y ~ 420 to 480
# Blue ring nozzle: X ~ 525 to 545, Y ~ 435 to 470
mixer_xs = []
mixer_ys = []
blue_xs = []
blue_ys = []
for y in range(400, 510):
    for x in range(370, 560):
        r, g, b = get_rgb(x, y)
        if b > 80 and b > r * 1.3 and b > g * 1.1:
            blue_xs.append(x)
            blue_ys.append(y)
        if 50 < r < 150 and abs(r-g) < 12 and abs(g-b) < 12:
            mixer_xs.append(x)
            mixer_ys.append(y)

print("=== MIXER T-200 ===")
print(f"Mixer total X span: min={min(mixer_xs)}, max={max(mixer_xs)}, width={max(mixer_xs)-min(mixer_xs)+1} px")
print(f"Mixer total Y span: min={min(mixer_ys)}, max={max(mixer_ys)}, height={max(mixer_ys)-min(mixer_ys)+1} px")
if blue_xs:
    print(f"Blue nozzle ring X span: min={min(blue_xs)}, max={max(blue_xs)}, width={max(blue_xs)-min(blue_xs)+1} px")
    print(f"Blue nozzle ring Y span: min={min(blue_ys)}, max={max(blue_ys)}, height={max(blue_ys)-min(blue_ys)+1} px")

# Inspect the vertical profiles width:
# Profile at Y=250:
for x in range(480, 510):
    r, g, b = get_rgb(x, 250)
    # check metal
# Let's check rail widths:
