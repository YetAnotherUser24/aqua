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

print("=== MEASURING ARROWS ===")
# 700 mm horizontal arrow
for y in range(590, 610):
    black_xs = [x for x in range(30, 620) if get_rgb(x, y) == (0, 0, 0)]
    if len(black_xs) > 100:
        print(f"700mm arrow line at Y={y}: min_x={min(black_xs)}, max_x={max(black_xs)}, span={max(black_xs)-min(black_xs)} px")
        break

# 600 mm vertical arrow
for x in range(35, 45):
    black_ys = [y for y in range(80, 560) if get_rgb(x, y) == (0, 0, 0)]
    if len(black_ys) > 100:
        print(f"600mm arrow line at X={x}: min_y={min(black_ys)}, max_y={max(black_ys)}, span={max(black_ys)-min(black_ys)} px")
        break

# 200 mm center arrow
black_ys_200 = [y for y in range(315, 530) if get_rgb(313, y) == (0, 0, 0)]
print(f"200mm arrow at X=313: min_y={min(black_ys_200)}, max_y={max(black_ys_200)}, span={max(black_ys_200)-min(black_ys_200)} px")

# 250 mm right arrow
black_ys_250 = [y for y in range(295, 550) if get_rgb(603, y) == (0, 0, 0)]
print(f"250mm arrow at X=603: min_y={min(black_ys_250)}, max_y={max(black_ys_250)}, span={max(black_ys_250)-min(black_ys_250)} px")

# 50 mm left arrow
black_ys_50l = [y for y in range(470, 530) if get_rgb(121, y) == (0, 0, 0)]
print(f"50mm left arrow at X=121: min_y={min(black_ys_50l)}, max_y={max(black_ys_50l)}, span={max(black_ys_50l)-min(black_ys_50l)} px")

# 50 mm right arrow
black_ys_50r = [y for y in range(470, 540) if get_rgb(552, y) == (0, 0, 0)]
print(f"50mm right arrow at X=552: min_y={min(black_ys_50r)}, max_y={max(black_ys_50r)}, span={max(black_ys_50r)-min(black_ys_50r)} px")

