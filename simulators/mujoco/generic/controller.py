#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Copyright 2016-present Neuraville Inc. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================
"""

import json
import sys
import time
import copy
import argparse
import threading
import numpy as np
import mujoco.viewer
from feagi_connector import retina
import xml.etree.ElementTree as ET
from feagi_connector import sensors
from feagi_connector import actuators
from feagi_connector import pns_gateway as pns
from feagi_connector.version import __version__
from feagi_connector import feagi_interface as feagi

RUNTIME = float('inf')  # (seconds) timeout time
SPEED = 120  # simulation step speed
xml_actuators_type = dict()

TRANSMISSION_TYPES = {
    'position': 'servo',
    'motor': 'motor'
}

SENSING_TYPES = {
    'framequat': 'gyro',
    'distance': 'proximity',
    'rangefinder': 'camera'
}


def action(obtained_data):
    recieve_servo_data = actuators.get_servo_data(obtained_data)
    recieve_servo_position_data = actuators.get_servo_position_data(obtained_data)

    if recieve_servo_position_data:
        # output like {0:0.50, 1:0.20, 2:0.30} # example but the data comes from your capabilities' servo range
        for real_id in recieve_servo_position_data:
            servo_number = real_id
            power = recieve_servo_position_data[real_id]
            if (len(data.ctrl) - 1) >= servo_number:
                data.ctrl[servo_number] = power

    if recieve_servo_data:
        # example output: {0: 0.245, 2: 1.0}
        for real_id in recieve_servo_data:
            servo_number = real_id
            new_power = recieve_servo_data[real_id]
            if (len(data.ctrl) - 1) >= servo_number:
                data.ctrl[servo_number] = new_power


def quaternion_to_euler(w, x, y, z):
    """Convert quaternion to euler angles (in degrees)"""
    # roll (x-axis rotation)
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = np.arctan2(sinr_cosp, cosr_cosp)

    # pitch (y-axis rotation)
    sinp = 2 * (w * y - z * x)
    if abs(sinp) >= 1:
        pitch = np.copysign(np.pi / 2, sinp)
    else:
        pitch = np.arcsin(sinp)

    # yaw (z-axis rotation)
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = np.arctan2(siny_cosp, cosy_cosp)

    return np.degrees([roll, pitch, yaw])


def get_head_orientation():
    # Get quaternion data from head sensor
    quat_id = model.sensor('head_gyro').id
    quat = data.sensordata[quat_id:quat_id + 4]  # w, x, y, z

    # Convert to euler angles
    euler_angles = quaternion_to_euler(quat[0], quat[1], quat[2], quat[3])

    return [euler_angles[0], euler_angles[1], euler_angles[2]]


def check_the_flag():
    parser = argparse.ArgumentParser(description="Load MuJoCo model from XML path")
    parser.add_argument(
        "--model_xml_path",
        type=str,
        default="./humanoid.xml",
        help="Path to the XML file (default: './humanoid.xml')"
    )

    args, remaining_args = parser.parse_known_args()

    path = args.model_xml_path
    model = mujoco.MjModel.from_xml_path(path)
    xml_info = get_actuators(path)
    xml_info = get_sensors(path, xml_info)
    print(f"Model loaded successfully from: {path}")

    cleaned_args = []
    skip_next = False
    for arg in sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg == "--model_xml_path":
            skip_next = True
        else:
            cleaned_args.append(arg)

    sys.argv = [sys.argv[0]] + cleaned_args
    return model, xml_info


def get_actuators(xml_path):
    # Parse the XML file
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Find the actuator section
    actuator_section = root.find('actuator')

    # Store actuator information in a dictionary
    actuators = {'output': {}}

    if actuator_section is not None:
        # Get all children of actuator section (all types of actuators)
        for actuator in actuator_section:
            name = actuator.get('name')
            actuators['output'][name] = {
                'type': actuator.tag}

    return actuators


def get_sensors(xml_path, sensors):
    # Parse the XML file
    tree = ET.parse(xml_path)
    root = tree.getroot()

    # Find the sensor section
    sensor_section = root.find('sensor')
    sensors['input'] = {}

    if sensor_section is not None:
        # Get all children of sensor section (all types of sensors)
        for sensor in sensor_section:
            name = sensor.get('name')
            sensors['input'][name] = {'type': sensor.tag}
    return sensors


if __name__ == "__main__":
    # Generate runtime dictionary
    runtime_data = {"vision": [], "stimulation_period": None, "feagi_state": None,
                    "feagi_network": None}

    # Step 3: Load the MuJoCo model
    model, xml_actuators_type = check_the_flag()
    print(xml_actuators_type)
    previous_frame_data = {}
    rgb = {}
    rgb['camera'] = {}
    camera_data = {"vision": {}}

    config = feagi.build_up_from_configuration()
    feagi_settings = config['feagi_settings'].copy()
    agent_settings = config['agent_settings'].copy()
    default_capabilities = config['default_capabilities'].copy()
    message_to_feagi = config['message_to_feagi'].copy()
    capabilities = config['capabilities'].copy()

    # Generate capabilities based off mujoco data
    data = mujoco.MjData(model)
    actuator_control_range = []  # this is to define the max power (motor), max value ( servo)
    actuator_information = {}
    sensor_information = {}

    for i in range(model.nu):
        actuator_name = model.actuator(i).name
        actuator_type = xml_actuators_type['output'][actuator_name]['type']
        actuator_information[actuator_name] = {"type": actuator_type, "range": model.actuator_ctrlrange[i]}
    print("\n\nactuator_information: ", actuator_information)

    for i in range(model.nsensor):
        sensor = model.sensor(i)
        # sensor_id = sensor.id # for device
        sensor_name = sensor.name
        test_sensor_type = sensor.type
        # sensor_data = data.sensordata[i]
        # print("sensor: ", " sensor name: ", sensor_name, " test type: ", test_sensor_type, " data: ", sensor_data)
        if test_sensor_type == 7:
            sensor_name = sensor_name[:-4]
        sensor_type = xml_actuators_type['input'][sensor_name]['type']
        sensor_information[sensor_name] = {"type": sensor_type}

    list_to_not_delete_device = []
    temp_copy_property_input = {}
    increment = 0
    # Reading sensors
    for mujoco_device_name in sensor_information:
        device_name = SENSING_TYPES.get(sensor_information[mujoco_device_name]['type'], None)
        if device_name in capabilities['input']:
            if device_name not in list_to_not_delete_device:
                increment = 0
                list_to_not_delete_device.append(device_name)
            elif device_name in list_to_not_delete_device:
                increment += 1
            device_id = str(increment)
            if increment == 0:
                temp_copy_property_input = copy.deepcopy(capabilities['input'][device_name][device_id])
            temp_copy_property_input['custom_name'] = mujoco_device_name
            temp_copy_property_input['feagi_index'] = increment
            capabilities['input'][device_name][device_id] = copy.deepcopy(temp_copy_property_input)

    temp_copy_property_output = {}
    increment = 0
    # Reading sensors
    for mujoco_device_name in actuator_information:
        device_name = TRANSMISSION_TYPES.get(actuator_information[mujoco_device_name]['type'], None)
        range_control = actuator_information[mujoco_device_name]['range']
        if device_name in capabilities['output']:
            if device_name not in list_to_not_delete_device:
                increment = 0
                list_to_not_delete_device.append(device_name)
            elif device_name in list_to_not_delete_device:
                increment += 1
            device_id = str(increment)
            if increment == 0:
                temp_copy_property_output = copy.deepcopy(capabilities['output'][device_name][device_id])
            if device_name == 'servo':
                temp_copy_property_output['max_value'] = range_control[1]
                temp_copy_property_output['min_value'] = range_control[0]
            elif device_name == 'motor':
                temp_copy_property_output['max_power'] = range_control[1]
                temp_copy_property_output['rolling_window_len'] = 2
            temp_copy_property_output['custom_name'] = mujoco_device_name
            temp_copy_property_output['feagi_index'] = increment
            capabilities['output'][device_name][device_id] = copy.deepcopy(temp_copy_property_output)

    temp_capabilities = copy.deepcopy(capabilities)
    for I_O in temp_capabilities:
        for device_name in temp_capabilities[I_O]:
            if device_name not in list_to_not_delete_device:
                del capabilities[I_O][device_name]


    # Write the modified capabilities to test.json
    with open("test.json", "w") as json_file:
        json.dump(capabilities, json_file, indent=4)




    print("END OF ACTUATOR")

    # # # FEAGI registration # # # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    feagi_settings, runtime_data, api_address, feagi_ipu_channel, feagi_opu_channel = \
        feagi.connect_to_feagi(feagi_settings, runtime_data, agent_settings, capabilities,
                               __version__)
    # - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -
    threading.Thread(target=retina.vision_progress,
                     args=(default_capabilities, feagi_settings, camera_data['vision'],),
                     daemon=True).start()
    default_capabilities = pns.create_runtime_default_list(default_capabilities, capabilities)

    # Create a dict to store data
    force_list = {}
    for x in range(20):
        force_list[str(x)] = [0, 0, 0]

    if 'servo' in capabilities['output']:
        actuators.start_servos(capabilities)
    if 'motor' in capabilities['output']:
        actuators.start_motors(capabilities)

    with mujoco.viewer.launch_passive(model, data) as viewer:
        mujoco.mj_resetDataKeyframe(model, data, 4)
        start_time = time.time()
        free_joints = [0] * 21  # keep track of which joints to lock and free (for unstable pause method)
        paused = True

        while viewer.is_running() and time.time() - start_time < RUNTIME:
            step_start = time.time()
            mujoco.mj_step(model, data)

            # The controller will grab the data from FEAGI in real-time
            message_from_feagi = pns.message_from_feagi
            if message_from_feagi:
                # Translate from feagi data to human readable data
                obtained_signals = pns.obtain_opu_data(message_from_feagi)
                # pns.check_genome_status_no_vision(message_from_feagi)
                action(obtained_signals)

            # ### actuator section
            # # Number of actuators
            # print("Number of actuators:", model.nu)
            # # Basic actuator states
            # print("Actuator controls (input signals):", data.ctrl)
            # print("Actuator forces:", data.actuator_force)
            # print("Actuator lengths:", data.actuator_length)
            # print("Actuator velocities:", data.actuator_velocity)
            # print("Actuator moments:", data.actuator_moment)
            # # Activation states (if using activation dynamics)
            # print("Activation states:", data.act)
            # print("Activation derivatives:", data.act_dot)
            # # Get actuator names from model
            # print("Actuator names:")
            # for i in range(model.nu):
            #     print(f"Actuator {i}: {model.actuator(i).name}")
            # # Get actuator types from model
            # print("Actuator types:")
            # for i in range(model.nu):
            #     print(f"Actuator {i} type:", model.actuator_trntype[i])
            # # Print actuator types
            # for i in range(model.nu):
            #     gain_type = model.actuator_gaintype[i]
            #     print(f"Bwuk Actuator {i} ({model.actuator(i).name}): {gain_type}")

            # print("Control ranges:")
            # for i in range(model.nu):
            #     actuator_name = model.actuator(i).name
            #     ctrl_range_min = model.actuator_ctrlrange[i][0]
            #     ctrl_range_max = model.actuator_ctrlrange[i][1]
            #     print(f"Actuator {i} ({actuator_name}):")
            #     print(f"  Control range: [{ctrl_range_min}, {ctrl_range_max}]")
            # print("END OF ACTUATOR")
            #
            # # Plugion section
            # print("PLUGIN SECTION")
            # # Number of plugins
            # print("Number of plugins:", data.nplugin)
            # # Print plugin data
            # print("Plugin data:", data.plugin_data)
            # # Print plugin state
            # print("Plugin state:", data.plugin_state)
            # # To see plugin instances directly
            # print("Plugin instances:")
            # for i in range(data.nplugin):
            #     print(f"Plugin {i}:", data.plugin(i))
            # print("Plugin details from model:")
            # for i in range(model.nplugin):
            #     plugin = model.plugin(i)
            #     print(f"Plugin {i}:")
            #     print(f"  Name: {plugin.name}")
            #     print(f"  Type: {plugin.type}")
            # #
            # # # Number of sensors
            # print("SENSOR LIST: ")
            # print("Number of sensors:", model.nsensor)
            # # Print sensor data
            # print("Sensor data:", data.sensordata)
            # # Print sensor names and types
            # print("Sensor details:")
            # for i in range(model.nsensor):
            #     sensor = model.sensor(i)
            #     print(f"Sensor {i}:")
            #     print(f"  Name: {sensor.name}")
            #     print(f"  Type: {sensor.type}")
            #     print(f"  Data value: {data.sensordata[i]}")
            #
            # # # region READ POSITIONAL DATA HERE ###
            # print([attr for attr in dir(data) if not attr.startswith('_')])
            # print(data.cam)
            #
            #
            # # # JOINT SECTION
            # print("JOINT SECTION HERE: ")
            # # Number of joints
            # print("Number of joints:", model.njnt)
            #
            # # Print joint positions (qpos) - but note the first 7 are the free joint as you mentioned
            # print("Joint positions:", data.qpos)
            #
            # # Print joint velocities
            # print("Joint velocities:", data.qvel)
            #
            # # Print detailed joint information
            # print("Joint details:")
            # for i in range(model.njnt):
            #     joint = model.joint(i)
            #     print(f"Joint {i}:")
            #     print(f"  Name: {joint.name}")
            #     print(f"  Type: {joint.type}")
            #     print(f"  Position: {data.joint(i)}")
            #     print(f"  qpos index: {joint.qposadr}")  # index into qpos array
            #     print(f"  qvel index: {joint.dofadr}")  # index into qvel array

            # # GEOM SECTION:
            # print("GEOMS SECTION:")
            # # Number of geoms
            # print("Number of geoms:", model.ngeom)
            #
            # # Print geom positions
            # print("\nGeom positions:", data.geom_xpos)
            #
            # # Print geom orientations (rotation matrices)
            # print("\nGeom orientations:", data.geom_xmat)
            #
            # # Print detailed geom information
            # print("\nGeom details:")
            # for i in range(model.ngeom):
            #     geom = model.geom(i)
            #     print(f"\nGeom {i}:")
            #     print(f"  Name: {geom.name}")
            #     print(
            #         f"  Type: {geom.type}")  # 0=plane, 1=hfield, 2=sphere, 3=capsule, 4=ellipsoid, 5=cylinder, 6=box, 7=mesh
            #     print(f"  Position: {data.geom_xpos[i]}")
            #     print(f"  Orientation: {data.geom_xmat[i]}")
            #     print(f"  Size: {geom.size}")  # dimensions depend on geom type
            #     print(f"  Mass: {geom.mass}")

            # # SITE INFORMATION
            # print("SITE SECTION: ")
            # # Number of sites
            # print("Number of sites:", model.nsite)
            #
            # # Print site positions
            # print("Site positions (xpos):", data.site_xpos)
            #
            # # Print site orientations (rotation matrices)
            # print("Site orientations (xmat):", data.site_xmat)
            #
            # # Print detailed site information
            # print("Site details:")
            # for i in range(model.nsite):
            #     site = model.site(i)
            #     print(f"Site {i}:")
            #     print(f"  Name: {site.name}")
            #     print(f"  Position: {data.site_xpos[i]}")
            #     print(f"  Orientation matrix: {data.site_xmat[i]}")

            ## CAMERA SECTION
            # # Print number of cameras
            # print("Number of cameras:", model.ncam)
            #
            # # Print camera names
            # for i in range(model.ncam):
            #     print(f"Camera {i} name:", model.camera(i).name)
            #
            # # Get camera positions
            # camera_positions = data.cam()
            # print("Camera positions:", camera_positions)

            positions = data.qpos  # all positions
            positions = positions[7:]  # don't know what the first 7 positions are, but they're not joints so ignore
            # them

            for i in range(data.ncon):
                force = np.zeros(6)  # Use numpy to allocate blank array

                # Retrieve the contact force data
                mujoco.mj_contactForce(model, data, i, force)
                obtained_data_from_force = force[:3]
                force_list[str(i)] = list((float(obtained_data_from_force[0]), float(obtained_data_from_force[1]),
                                           float(obtained_data_from_force[2])))
            # endregion

            # Pick up changes to the physics state, apply perturbations, update options from GUI.
            viewer.sync()

            # Tick Speed #
            time_until_next_step = (1 / SPEED) - (time.time() - step_start)
            if time_until_next_step > 0:
                time.sleep(time_until_next_step)

            # Example to send data to FEAGI. This is basically reading the joint.

            # servo_data = {i: pos for i, pos in enumerate(positions[:20]) if
            #               pns.full_template_information_corticals}
            # sensor_data = {i: pos for i, pos in enumerate(data.sensordata[3:6]) if
            #                pns.full_template_information_corticals}
            # lidar_data = {i: pos for i, pos in enumerate(data.sensordata[7:]) if
            #                pns.full_template_information_corticals}
            # lidar_data = data.sensordata[7:] * 100
            # lidar_2d = lidar_data.reshape(16, 16)
            #
            # # Create 16x16x3 array and flatten it
            # result = np.zeros((16, 16, 3))  # 3 for x,y,z
            # result[:, :, 0] = lidar_2d  # Set first channel to LIDAR data
            # flat_result = result.flatten()  # Makes it 1D array of length 768 (16*16*3)
            # raw_frame = retina.RGB_list_to_ndarray(flat_result,
            #                                        [16, 16])
            # camera_data['vision'] = {"0": retina.update_astype(raw_frame)}

            # previous_frame_data, rgb, default_capabilities = \
            #     retina.process_visual_stimuli(
            #         camera_data['vision'],
            #         default_capabilities,
            #         previous_frame_data,
            #         rgb, capabilities)
            # message_to_feagi = pns.generate_feagi_data(rgb, message_to_feagi)
            #
            # # Get gyro data
            # gyro = get_head_orientation()
            # gyro_data = {"0": np.array(gyro)}

            # Creating message to send to FEAGI
            # message_to_feagi = sensors.create_data_for_feagi('gyro',
            #                                                  capabilities,
            #                                                  message_to_feagi,
            #                                                  current_data=gyro_data,
            #                                                  symmetric=True)
            # message_to_feagi = sensors.create_data_for_feagi('servo_position',
            #                                                  capabilities,
            #                                                  message_to_feagi,
            #                                                  current_data=servo_data,
            #                                                  symmetric=True)
            #
            # message_to_feagi = sensors.create_data_for_feagi('proximity',
            #                                                  capabilities,
            #                                                  message_to_feagi,
            #                                                  current_data=sensor_data,
            #                                                  symmetric=True, measure_enable=True)
            # message_to_feagi = sensors.create_data_for_feagi('pressure',
            #                                                  capabilities,
            #                                                  message_to_feagi,
            #                                                  current_data=force_list,
            #                                                  symmetric=True,
            #                                                  measure_enable=False)  # measure enable set to false so
            # that way, it doesn't change 50/-50 in capabilities automatically

            # Sends to feagi data
            pns.signals_to_feagi(message_to_feagi, feagi_ipu_channel, agent_settings, feagi_settings)
            message_to_feagi.clear()
