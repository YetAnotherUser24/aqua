# Compute exact CAD dimensions in mm
# Single source of truth: real_CAD.png (645 x 636)

# Scales:
# Width: 533 px = 700 mm -> 0.76143 px/mm (1.3133 mm/px)
# Height: 451 px = 600 mm -> 0.75167 px/mm (1.3304 mm/px)

X_SCALE = 533.0 / 700.0
Y_SCALE = 451.0 / 600.0

# Tank Origin:
# Left inside wall = 56 px (0 mm)
# Right inside wall = 589 px (700 mm)
# Inside floor = 546 px (0 mm)
# Top rim = 95 px (600 mm)

def px_to_mm(px, py):
    x_mm = (px - 56) / X_SCALE
    y_mm = (546 - py) / Y_SCALE
    return x_mm, y_mm

# Motor canister:
# X: [248, 370] -> width = 122 px -> 160 mm
# Center X = 309 px -> 332 mm
# Y: [100, 215] -> height = 115 px -> 153 mm
# Y bottom = 215 px -> 440 mm from floor
# Y top = 100 px -> 593 mm from floor
print("Motor canister:")
x1, y1 = px_to_mm(248, 215)
x2, y2 = px_to_mm(370, 100)
print(f"  X: {x1:.1f} to {x2:.1f} mm (w={x2-x1:.1f} mm), Y: {y1:.1f} to {y2:.1f} mm (h={y2-y1:.1f} mm)")

# Shaft:
# X: [305, 320] -> width = 15 px -> 19.7 mm
# Y: [215, 282] -> length = 67 px -> 89.1 mm
print("Shaft:")
sx1, sy1 = px_to_mm(305, 282)
sx2, sy2 = px_to_mm(320, 215)
print(f"  X: {sx1:.1f} to {sx2:.1f} mm (diam={sx2-sx1:.1f} mm), Y: {sy1:.1f} to {sy2:.1f} mm (len={sy2-sy1:.1f} mm)")

# Impeller:
# Collar: Y=[282, 288] (6 px = 8 mm), width=[301, 324] (23 px = 30 mm)
# Hub bottom: Y=319 -> 302 mm from floor
# Blades top: Y=288 -> 343 mm from floor
# Blades width: X=[256, 367] (111 px = 145.8 mm, radius = 72.9 mm)
# Impeller height: 319 - 288 = 31 px = 41.2 mm
print("Impeller:")
ix1, iy1 = px_to_mm(256, 319)
ix2, iy2 = px_to_mm(367, 288)
print(f"  Width: {ix2-ix1:.1f} mm, Height: {iy2-iy1:.1f} mm")
print(f"  Hub bottom Y: {iy1:.1f} mm from floor, Blades top Y: {iy2:.1f} mm from floor")

# Thruster T-200:
# Clamp upright rail: X=495 px -> 576 mm
# Clamp Y: [400, 509] -> Y in [49.2, 194.2] mm from floor (height = 145 mm)
# Main cylinder: X=[370, 495] -> length = 125 px -> 164 mm, diameter = 50 px -> 66.5 mm
# Blue nozzle: X=[532, 538] -> 7 px -> 9.2 mm, diameter = 24 px -> 32 mm
print("Thruster T-200:")
tx1, ty1 = px_to_mm(370, 480)
tx2, ty2 = px_to_mm(495, 420)
print(f"  Cylinder X: {tx1:.1f} to {tx2:.1f} mm (len={tx2-tx1:.1f} mm), diam: {ty2-ty1:.1f} mm")
print(f"  Nozzle diam: 32 mm")

# Sensor OPTOD:
# Rail: X=135 px -> 103.8 mm
# Sensor body: X=[140, 163] -> 23 px -> 30 mm
# Sensor Y: [350, 491] -> tip at 491 px -> 73 mm from floor (or 50 mm nominal cota)
# Sensor body height: 491 - 350 = 141 px -> 187 mm
print("Sensor OPTOD:")
px1, py1 = px_to_mm(140, 491)
px2, py2 = px_to_mm(163, 350)
print(f"  X: {px1:.1f} to {px2:.1f} mm, tip Y: {py1:.1f} mm, height: {py2-py1:.1f} mm")

