"""Checks that a cube falls onto the ground and the camera renders an image."""

import faulthandler
import json
import pathlib

import numpy as np
from isaacsim import SimulationApp

faulthandler.dump_traceback_later(180, repeat=True)
app = SimulationApp({"headless": True, "width": 640, "height": 480, "multi_gpu": False})

try:
    import isaaclab
    import isaacsim.core.api as core_api
    import isaacsim.core.api.objects as objects
    import isaacsim.sensors.camera as camera_module
    import torch
    from pxr import UsdLux

    assert torch.arange(16, device="cuda").sum().item() == 120
    print("Isaac Lab:", isaaclab.__file__, flush=True)

    world = core_api.World(stage_units_in_meters=1.0)
    world.scene.add_default_ground_plane()
    light = UsdLux.DomeLight.Define(world.stage, "/World/light")
    light.CreateIntensityAttr(1000.0)
    cube = world.scene.add(
        objects.DynamicCuboid(
            prim_path="/World/cube",
            name="cube",
            position=np.array([0.0, 0.0, 1.0]),
            size=0.3,
            color=np.array([0.9, 0.2, 0.1]),
        )
    )
    camera = camera_module.Camera(
        prim_path="/World/camera",
        position=np.array([0.0, 0.0, 4.0]),
        orientation=np.array([2**-0.5, 0.0, 2**-0.5, 0.0]),
        resolution=(128, 128),
    )
    world.reset()
    camera.initialize()
    for step in range(120):
        if step % 30 == 0:
            print("Rendering step", step, flush=True)
        world.step(render=True)
    rgba = camera.get_rgba()
    position, _ = cube.get_world_pose()
    assert rgba.shape == (128, 128, 4), rgba.shape
    assert np.isfinite(rgba).all()
    assert rgba[:, :, :3].std() > 0, "Camera returned a constant image"
    assert 0.05 < position[2] < 0.4, position
    output = pathlib.Path("/output")
    np.save(output / "camera.npy", rgba)
    result = {"camera_shape": list(rgba.shape), "cube_z": float(position[2])}
    (output / "smoke.json").write_text(json.dumps(result, indent=2))
    print("SIMPROTO_SMOKE_PASS", result, flush=True)
finally:
    faulthandler.cancel_dump_traceback_later()
    app.close()
