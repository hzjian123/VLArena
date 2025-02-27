import base64
from datetime import datetime
import io
import json
import os
import sys
import time
import math
from typing import Dict, List, Optional, Tuple
import dearpygui.dearpygui as dpg
from matplotlib import pyplot as plt
import requests
import numpy as np
import torch
import cv2
from io import BytesIO
import yaml
from PIL import Image, ImageDraw, ImageFont
# Add LimSim to sys.path
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "LimSim"))  # noqa
from TrafficManager.utils.sim_utils import limsim2diffusion, normalize_angle, transform_to_ego_frame, interpolate_traj
from TrafficManager.utils.map_utils import VectorizedLocalMap
from LimSim.utils.trajectory import Trajectory, State
from LimSim.trafficManager.traffic_manager import TrafficManager
from LimSim.simModel.MPGUI import GUI
from LimSim.simModel.Model import Model
from LimSim .simModel.DataQueue import CameraImages
from TrafficManager.utils.matplot_render import MatplotlibRenderer
from LimSim.simInfo.CustomExceptions import CollisionChecker, OffRoadChecker
from TrafficManager.utils.scorer import Scorer
from vlm_utils import add_interpolate_traj,world_to_ego
from collections import deque
ports = [11000,11002]
# Use lsof to check which process is using the port and kill the first one foundt
if 0:
    for port in ports:
        result = os.popen(f"lsof -i :{port}").read()
        lines = result.splitlines()
        # Skip the header line and find the PID
        if len(lines) > 1:
            pid_line = lines[1]  # First line after the header
            pid = pid_line.split()[1]  # PID is the second column

            # Kill the process
            os.system(f"kill -9 {pid}")
            f"Process {pid} using port {port} has been terminated."
        else:
            "No process found using port {port}."
    exit()

def parse_trajectory(traj_string):
    # Convert a trajectory string into a list of (x, y) tuples
    try:
        points = traj_string.strip("()").split("),(")
        points = [list(map(float, point.strip().split(","))) for point in points]
        points = [p[::-1] for p in points]
        return points
    except:
        print('Invalid output from VLM!!!!!!!!!!! Use ZERO traj as output',traj_string)
        return [[0.0,0.0] for _ in range(6)] # [[0,0],[0,2],[0,5],[0,7],[0,9],[0,10]]
class SimulationManager:
    def __init__(self, config_path: str):
        self.config = self.load_config(config_path)
        self.setup_constants()
        self.setup_paths()
        self.model: Optional[Model] = None
        self.planner: Optional[TrafficManager] = None
        self.vectorized_map: Optional[VectorizedLocalMap] = None
        self.gui: Optional[GUI] = None
        self.renderer: Optional[MatplotlibRenderer] = None
        self.checkers: List = []
        self.scorer: Optional[Scorer] = None
        self.timestamp: float = -0.5
        self.data_template: Optional[torch.Tensor] = None
        self.last_pose: torch.Tensor = torch.eye(4)
        self.accel: List[float] = [0, 0, 9.80]
        self.rotation_rate: List[float] = [0, 0, 0]
        self.vel: List[float] = [0, 0, 0]
        self.agent_command: int = 2  # Defined by UniAD  0: Right 1:Left 2:Forward
        self.result_path = f"./results/{datetime.now().strftime('%m-%d-%H%M%S')}/"
        self.img_save_path = f"{self.result_path}imgs/"
        os.makedirs(self.result_path, exist_ok=True)
        os.makedirs(self.img_save_path, exist_ok=True)

    @staticmethod
    def load_config(config_path: str) -> Dict:
        with open(config_path, 'r') as config_file:
            return yaml.safe_load(config_file)

    def setup_constants(self):
        self.DIFFUSION_SERVER = self.config['servers']['diffusion']
        self.DRIVER_SERVER = self.config['servers']['vlm_driver']
        self.USE_AGENT_PATH =  self.config['simulation']['use_agent_path']
        self.STEP_LENGTH = self.config['simulation']['step_length']
        self.GUI_DISPLAY = self.config['simulation']['gui_display']
        self.MAX_SIM_TIME = self.config['simulation']['max_sim_time']
        self.EGO_ID = self.config['simulation']['ego_id']
        self.MAP_NAME = self.config['map']['name']
        self.GEN_PROMPT = self.config['map']['gen_description']
        self.IMAGE_SIZE = self.config['image']['size']
        self.TARGET_SIZE = tuple(self.config['image']['target_size'])

    def setup_paths(self):
        data_root = os.path.dirname(os.path.abspath(__file__))
        self.SUMO_CFG_FILE = os.path.join(
            data_root, self.config['map']['sumo_cfg_file'].format(map_name=self.MAP_NAME))
        self.SUMO_NET_FILE = os.path.join(
            data_root, self.config['map']['sumo_net_file'].format(map_name=self.MAP_NAME))
        self.SUMO_ROU_FILE = os.path.join(
            data_root, self.config['map']['sumo_rou_file'].format(map_name=self.MAP_NAME))
        self.DATA_TEMPLATE_PATH = os.path.join(
            data_root, self.config['data']['template_path'])
        self.NU_SCENES_DATA_ROOT = os.path.join(
            data_root, self.config['data']['nu_scenes_root'].format(map_name=self.MAP_NAME))

    @staticmethod
    def normalize_angle(angle: float) -> float:
        return (angle + math.pi) % (2 * math.pi) - math.pi

    def send_request_diffusion(self, diffusion_data: Dict) -> Optional[np.ndarray]:
        serialized_data = {
            k: v.numpy().tolist() if isinstance(v, torch.Tensor) else
            {k2: v2.numpy().tolist() if isinstance(v2, torch.Tensor) else v2 for k2, v2 in v.items()} if isinstance(v, dict) else
            v.tolist() if isinstance(v, np.ndarray) else v
            for k, v in diffusion_data.items()
        }

        try:
            print(f"Sending data to WorldDreamer server...")
            response = requests.post(
                self.DIFFUSION_SERVER + "dreamer-api/", json=serialized_data)
            #if response.status_code == 200 and 'image' in response.headers['Content-Type']:
            if response.status_code == 200 and 'application/json' in response.headers['Content-Type']:
                content = response.json()
                #print(content.keys())#dict_keys(['timestamp', 'img_byte_array', 'ego_pose', 'command', 'accel', 'rotation_rate', 'vel'])
                img_data = base64.b64decode(content['img_byte_array'])
                image = Image.open(BytesIO(img_data))
                images_array = np.array(np.split(np.array(image), 6, axis=0))
                combined_image = np.vstack(
                    (np.hstack(images_array[:3]), np.hstack(images_array[3:][::-1])))
                cv2.imwrite(f"{self.img_save_path}diffusion_{str(int(self.timestamp*2)).zfill(3)}.jpg",
                            cv2.cvtColor(combined_image, cv2.COLOR_RGB2BGR))
                return content,np.array(np.split(np.array(image), 6, axis=0))
        except requests.exceptions.RequestException as e:
           print(f"Warning: Request failed due to {e}")
        return None,None
    # def send_request_driver(self, content: Dict) -> Optional[np.ndarray]:
    #     try:
    #         print(f"Sending data to WorldDreamer server...")
    #         response = requests.post(
    #             self.DIFFUSION_SERVER + "driver-api/", json=content)
    #         #if response.status_code == 200 and 'image' in response.headers['Content-Type']:
    #         if response.status_code == 200 and 'application/json' in response.headers['Content-Type']:
    #             content = response.json()
    #             #print(content.keys())#dict_keys(['timestamp', 'img_byte_array', 'ego_pose', 'command', 'accel', 'rotation_rate', 'vel'])
    #             img_data = base64.b64decode(content['img_byte_array'])
    #             image = Image.open(BytesIO(img_data))
    #             images_array = np.array(np.split(np.array(image), 6, axis=0))
    #             combined_image = np.vstack(
    #                 (np.hstack(images_array[:3]), np.hstack(images_array[3:][::-1])))
    #             cv2.imwrite(f"{self.img_save_path}diffusion_{str(int(self.timestamp*2)).zfill(3)}.jpg",
    #                         cv2.cvtColor(combined_image, cv2.COLOR_RGB2BGR))
    #             return np.array(np.split(np.array(image), 6, axis=0))
    #     except requests.exceptions.RequestException as e:
    #        print(f"Warning: Request failed due to {e}")
    #     return None
    def get_drivable_mask(self, model: Model) -> np.ndarray:
        img = np.zeros((self.IMAGE_SIZE, self.IMAGE_SIZE), dtype=np.uint8)
        roadgraphRenderData, VRDDict = model.renderQueue.get()
        egoVRD = VRDDict['egoCar'][0]
        ex, ey, ego_yaw = egoVRD.x, egoVRD.y, egoVRD.yaw

        OffRoadChecker().draw_roadgraph(img, roadgraphRenderData, ex, ey, ego_yaw)
        return img.astype(bool)

    def initialize_simulation(self):
        # Initialising models, planners, maps etc
        self.model = Model(
            egoID=self.EGO_ID, netFile=self.SUMO_NET_FILE, rouFile=self.SUMO_ROU_FILE,
            cfgFile=self.SUMO_CFG_FILE, dataBase=self.result_path+"limsim.db", SUMOGUI=False,
            CARLACosim=False,
        )
        self.model.start()
        self.planner = TrafficManager(
            self.model, config_file_path='./TrafficManager/LimSim/trafficManager/config.yaml')

        print(f"Testing connection to WorldDreamer & Driver servers...")
        requests.get(self.DIFFUSION_SERVER + "dreamer-clean/")
        requests.get(self.DRIVER_SERVER + "driver-clean/")

        self.data_template = torch.load(self.DATA_TEMPLATE_PATH)
        self.vectorized_map = VectorizedLocalMap(
            dataroot=self.NU_SCENES_DATA_ROOT, map_name=self.MAP_NAME, patch_size=[100, 100], fixed_ptsnum_per_line=-1)

        self.gui = GUI(self.model)
        if self.GUI_DISPLAY:
            self.gui.start()

        self.renderer = MatplotlibRenderer()
        self.checkers = [OffRoadChecker(), CollisionChecker()]
        x_val = deque(maxlen=7)
        y_val = deque(maxlen=7)
        self.past_traj = [x_val,y_val]
        self.ego_past_traj = []

    def process_frame(self):
        # Single frame processing logic
        if self.scorer is None:
            self.scorer = Scorer(self.model, map_name=self.MAP_NAME,
                                 save_file_path=self.result_path+"drive_arena.pkl")
        try:
            for checker in self.checkers:
                checker.check(self.model)
        except Exception as e:
            print(
                f"WARNING: Checker failed @ timestep {self.model.timeStep}. {e}")
            raise e
        drivable_mask = self.get_drivable_mask(self.model)
        if self.model.timeStep % 5 == 0:
            self.timestamp += 0.5
            if self.timestamp >= self.MAX_SIM_TIME:
                print("Simulation time end.")
                return False

            limsim_trajectories = self.planner.plan(
                self.model.timeStep * 0.1, self.roadgraph, self.vehicles)
            if not limsim_trajectories[self.EGO_ID].states:
                return True

            traj_len = min(
                len(limsim_trajectories[self.EGO_ID].states) - 1, 25)
            local_x, local_y, local_yaw = transform_to_ego_frame(
                limsim_trajectories[self.EGO_ID].states[0], limsim_trajectories[self.EGO_ID].states[traj_len])
            self.agent_command = 2 if local_x <= 5.0 else (
                1 if local_y > 4.0 else 0 if local_y < -4.0 else 2)
            print("Agent command:", self.agent_command)

            diffusion_data = limsim2diffusion(
                self.vehicles, self.data_template, self.vectorized_map, self.MAP_NAME, self.agent_command, self.last_pose, drivable_mask,
                self.accel, self.rotation_rate, self.vel,
                gen_location=self.MAP_NAME,
                gen_prompts=self.GEN_PROMPT,
            )
            self.last_pose = diffusion_data['metas']['ego_pos']
            content,gen_images = self.send_request_diffusion(diffusion_data)
            #print('C'*200)#dict_keys(['timestamp', 'img_byte_array', 'ego_pose', 'command', 'accel', 'rotation_rate', 'vel'])
            pose = np.array(content['ego_pose'])
            from scipy.spatial.transform import Rotation as R
            rotation_matrix = pose[:3, :3]
            # Convert to quaternion
            r = R.from_matrix(rotation_matrix)
            quat = r.as_quat()  # Returns [x, y, z, w]
            cmd_map = {0: "Turn Right", 1: "Turn Left", 2: "Go Straight."}
            command =  cmd_map[content['command']]# 0: Right 1:Left 2:Forward
            quat[0],quat[2]= quat[2],quat[0]#TODO check coord correct???
            quat = [round(q,2) for q in quat]
            #content['accel'] = ([round(a,2) for a in content['accel']])# frot ,left, down
            #content['rotation_rate'] = ([round(r,2) for r in content['rotation_rate']])
            #content['vel']= ([round(v,2) for v in content['vel']])
            content['ego_pose'] = quat
            content['command'] = command
            content['past_traj'] = self.ego_past_traj
            #print(content['accel'], content['rotation_rate'], content['vel'])
            #self.send_request_driver(content)
            if gen_images is not None:
                front_left_image, front_image, front_right_image = [
                    Image.fromarray(img).convert('RGBA') for img in gen_images[:3]]
            else:
                raise ValueError("No images generated!")
            new_width, new_height = self.TARGET_SIZE[0], int(
                (self.TARGET_SIZE[0] / front_image.width) * front_image.height)
            resized_images = [img.resize((new_width, new_height), Image.Resampling.LANCZOS) for img in [
                front_left_image, front_image, front_right_image]]

            ci = CameraImages()
            ci.CAM_FRONT_LEFT, ci.CAM_FRONT, ci.CAM_FRONT_RIGHT = [
                np.array(img) for img in resized_images]
            print("Current timestamp:", self.timestamp)
            response = requests.post(
                self.DRIVER_SERVER + "driver-api/", json=content)
            #response = requests.get(self.DRIVER_SERVER + "driver-get/")
            while response.status_code != 200 or response.text == "false":
                # print("The Driver Agent not processing done, try again in 1s")
                time.sleep(0.5)
                #response = requests.get(self.DRIVER_SERVER + "driver-get/")
                response = requests.post(
                    self.DRIVER_SERVER + "driver-api/", json=content)
                # print("Driver Agent", response.status_code)
            #TODO Outputs from UniAD
            driver_output = json.loads(response.text)['traj']
            traj = parse_trajectory(driver_output)
            traj = [[-point[0],point[1]] for point in traj]
            print("Driver Agent's Path:", traj)

            traj.insert(0, [0.0, 0.0])
            ego_vehicle = self.vehicles['egoCar']
            self.past_traj[0].append(ego_vehicle["xQ"][-1])
            self.past_traj[1].append(ego_vehicle["yQ"][-1])
            ego_traj = interpolate_traj(ego_vehicle, traj)


            if len(limsim_trajectories[self.EGO_ID].states) < 10:
                yaw_rate = 0
            else:
                yaw_rate = limsim_trajectories[self.EGO_ID].states[9].yaw - \
                    limsim_trajectories[self.EGO_ID].states[0].yaw
            vx_1, vx_2 = traj[2][0] - \
                traj[0][0], traj[3][0] - traj[1][0]
            vy_1, vy_2 = traj[2][1] - \
                traj[0][1], traj[3][1] - traj[1][1]
            ax, ay = (vx_2 - vx_1) / 0.5, (vy_2 - vy_1) / 0.5
            self.accel = [ax, ay, 9.80]#assume no z axis accel
            self.rotation_rate = [0, 0, yaw_rate]#only yaw angle
            tot_vel = limsim_trajectories[self.EGO_ID].states[0].vel
            self.vel = [tot_vel, 0, 0]#total v instead of in x,y axis
            v_x = tot_vel * math.cos(yaw_rate)  # Velocity in the x direction
            v_y = tot_vel * math.sin(yaw_rate)  # Velocity in the y direction
            #print('V'*100,self.vel,len(limsim_trajectories[self.EGO_ID].states),[q.t for q in limsim_trajectories[self.EGO_ID].states])
            print("Accel:", self.accel, "\nRotation rate:",
                  self.rotation_rate, "\nVel:", self.vel)
            self.model.putRenderData()
            roadgraphRenderData, VRDDict = self.model.renderQueue.get()
            img_path = f'{self.img_save_path}bev_{str(int(self.timestamp*2)).zfill(3)}.png'
            self.renderer.render(roadgraphRenderData, VRDDict,
                                 img_path)
            image = Image.open(img_path)
            draw = ImageDraw.Draw(image)
            # Define the font and size (ensure the font file is available in your path)
            font = ImageFont.load_default(180)
            # Set the position for the text (bottom-left corner)
            text_position = (500,image.height-500)

            # Draw the text
            draw.text(text_position, command, font=font, fill="black") 
            # traj = custom_interpolate_traj(ego_vehicle, path_points)# convert to world coordinate
            # traj = [[-point[0],-point[1]] for point in traj]
            add_interpolate_traj(draw, image.width,traj,self.ego_past_traj)
            self.ego_past_traj = world_to_ego(self.past_traj,ego_vehicle)
            #add_traj(draw,image.width,traj)
            image.save(img_path)
            self.scorer.record_frame(drivable_mask, is_planning_frame=True,
                                     planned_traj=ego_traj, ref_traj=limsim_trajectories[self.EGO_ID])

            limsim_trajectories = {}
            if self.USE_AGENT_PATH and self.timestamp > 2.5:
                ## Because first 3 seconds, drive agents may not ready
                print(f"Use agent path to drive.")
                limsim_trajectories[self.EGO_ID] = ego_traj
            self.model.setTrajectories(limsim_trajectories)
        else:
            if self.scorer is not None:
                self.scorer.record_frame(
                    drivable_mask, is_planning_frame=False)

        return True
    def run_simulation(self):
        self.initialize_simulation()
        try:
            while not self.model.tpEnd:
                self.model.moveStep()
                self.roadgraph, self.vehicles = self.model.exportSce()
                if self.vehicles and 'egoCar' in self.vehicles:
                    if not self.process_frame():
                        break
                self.model.updateVeh()
        finally:
            self.cleanup()

    def cleanup(self):
        print("Simulation ends")
        if self.scorer:
            self.scorer.save()
        self.model.destroy()
        self.gui.terminate()
        self.gui.join()


def main():
    sim_manager = SimulationManager('./TrafficManager/config.yaml')
    sim_manager.run_simulation()


if __name__ == '__main__':
    main()
