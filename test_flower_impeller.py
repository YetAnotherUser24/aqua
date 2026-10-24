import math

# Test the upward-opening flower geometry mathematically
R = 73.0 # mm
H = 41.0 # mm
collar_r = 15.0 # mm
hub_bot_r = 14.0 # mm

def blade_profile(r_ratio):
    # r_ratio in [0, 1]
    # At r_ratio = 0: y = 0 (bottom of hub)
    # At r_ratio = 1: y = H (top rim of blades)
    # Concave upward curve: like a flower opening upwards
    return H * (r_ratio ** 1.6)

print("Blade height at various radii from center to tip:")
for r_frac in [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]:
    r = r_frac * R
    h = blade_profile(r_frac)
    print(f"  r = {r:5.1f} mm: h = {h:5.1f} mm (curves upwards!)")

