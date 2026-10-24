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

print("=== IMPELLER PROFILE BY Y-LINE ===")
for y in range(280, 325):
    # Find all reddish/brownish pixels of the impeller
    xs = []
    for x in range(240, 385):
        r, g, b = get_rgb(x, y)
        if r > 90 and r > g * 1.15 and r > b * 1.15:
            xs.append(x)
    if xs:
        print(f"Y={y}: min_x={min(xs)}, max_x={max(xs)}, width={max(xs)-min(xs)+1}, xs_count={len(xs)}")
    else:
        print(f"Y={y}: None")
