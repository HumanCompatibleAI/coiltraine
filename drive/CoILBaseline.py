import numpy as np
import scipy
import sys
import math
import os
import glob
import torch

from scipy.misc import imresize
from PIL import Image

import matplotlib.pyplot as plt

from utils.drive_utils import checkpoint_parse_configuration_file
from configs import g_conf, merge_with_yaml
from network import CoILModel

try:
    sys.path.append(glob.glob('**/carla-*%d.%d-%s.egg' % (
        sys.version_info.major,
        sys.version_info.minor,
        'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass

# CARLA ROOT can probably be erased

try:
    CARLA_ROOT = os.environ.get('CARLA_ROOT')
    if not CARLA_ROOT:
        print('Warning! Define environment variable CARLA_ROOT pointing to the CARLA base folder.')

    sys.path.append(glob.glob('{}/PythonAPI'.format(CARLA_ROOT))[0])
except IndexError:
    pass
# We  need to add two things here to the python path,

from srunner.challenge.autoagents.autonomous_agent import AutonomousAgent
from agents.navigation.local_planner import RoadOption

import carla


def distance_vehicle(waypoint, vehicle_position):

    dx = waypoint['lat'] - vehicle_position[0]
    dy = waypoint['lon'] - vehicle_position[1]

    return math.sqrt(dx * dx + dy * dy)


class CoILBaseline(AutonomousAgent):

    def setup(self, path_to_config_file):

        config_file = "file.xml"
        yaml_conf, checkpoint_number = checkpoint_parse_configuration_file(config_file)

        # Take the checkpoint name and load it
        checkpoint = torch.load(os.path.join('/', os.path.join(*os.path.realpath(__file__).split('/')[:-2]),
                                              '_logs',
                                             yaml_conf.split('/')[-2], yaml_conf.split('/')[-1].split('.')[-2]
                                             , 'checkpoints', str(checkpoint_number) + '.pth'))

        # do the merge here
        merge_with_yaml(os.path.join('/', os.path.join(*os.path.realpath(__file__).split('/')[:-2]),
                                     yaml_conf))


        self.checkpoint = checkpoint  # We save the checkpoint for some interesting future use.
        self._model = CoILModel(g_conf.MODEL_TYPE, g_conf.MODEL_CONFIGURATION)
        self.first_iter = True
        # Load the model and prepare set it for evaluation
        self._model.load_state_dict(checkpoint['state_dict'])
        self._model.cuda()
        self._model.eval()
        self.latest_image = None
        self.latest_image_tensor = None


    def sensors(self):

        sensors = [{'type': 'sensor.camera.rgb',
                   'x': 2.0, 'y': 0.0,
                    'z': 1.40, 'roll': 0.0,
                    'pitch': -15.0, 'yaw': 0.0,
                    'width': 800, 'height': 600,
                    'fov': 100,
                    'id': 'rgb'},
                   {'type': 'sensor.speedometer',
                    'reading_frequency': 25,
                    'id': 'speed'
                    },
                   {'type': 'sensor.other.gnss',
                    'x': 0.7, 'y': -0.4, 'z': 1.60,
                    'id': 'GPS'}
                   ]

        return sensors

    def run_step(self, input_data):

        # measurements, sensor_data, directions, target
        print("=====================>")
        for key, val in input_data.items():
            shape = val[1].shape
            print("[{} -- {:06d}] with shape {}".format(key, val[0], shape))
        print("<=====================")
        print ("speed: ", input_data['speed'])
        print ("gps: ", input_data['GPS'])

        directions = self._get_current_direction(input_data['GPS'][1])

        print ("Directions : ", directions)
        # Take the forward speed and normalize it for it to go from 0-1
        norm_speed = input_data['speed'][1] / g_conf.SPEED_FACTOR
        # norm_speed = 0.2
        norm_speed = torch.cuda.FloatTensor([norm_speed]).unsqueeze(0)
        directions_tensor = torch.cuda.LongTensor([directions])
        # Compute the forward pass processing the sensors got from CARLA.
        model_outputs = self._model.forward_branch(self._process_sensors(input_data['rgb'][1]),
                                                   norm_speed,
                                                   directions_tensor)

        steer, throttle, brake = self._process_model_outputs(model_outputs[0])

        control = carla.VehicleControl()
        control.steer = float(steer)
        control.throttle = float(throttle)
        control.brake = float(brake)
        # There is the posibility to replace some of the predictions with oracle predictions.
        self.first_iter = False
        return control

    def set_global_plan(self, topological_plan):
        # We expand the commands before the curves in order to give more time
        # for the agent to respond.
        topological_plan = self._expand_commands(topological_plan)
        self._global_plan = topological_plan

    def get_attentions(self, layers=None):
        """
        Returns
            The activations obtained from the first layers of the latest iteration.

        """
        if layers is None:
            layers = [0, 1, 2]
        if self.latest_image_tensor is None:
            raise ValueError('No step was ran yet. '
                             'No image to compute the activations, Try Running ')
        all_layers = self._model.get_perception_layers(self.latest_image_tensor)
        cmap = plt.get_cmap('inferno')
        attentions = []
        for layer in layers:
            y = all_layers[layer]
            att = torch.abs(y).mean(1)[0].data.cpu().numpy()
            att = att / att.max()
            att = cmap(att)
            att = np.delete(att, 3, 2)
            attentions.append(imresize(att, [88, 200]))
        return attentions

    def _process_sensors(self, sensor):

        iteration = 0

        sensor = sensor[g_conf.IMAGE_CUT[0]:g_conf.IMAGE_CUT[1], ...]

        sensor = scipy.misc.imresize(sensor, (g_conf.SENSORS['rgb'][1], g_conf.SENSORS['rgb'][2]))

        self.latest_image = sensor

        sensor = np.swapaxes(sensor, 0, 1)

        sensor = np.transpose(sensor, (2, 1, 0))

        sensor = torch.from_numpy(sensor / 255.0).type(torch.FloatTensor).cuda()

        if iteration == 0:
            image_input = sensor

        iteration += 1

        image_input = image_input.unsqueeze(0)

        self.latest_image_tensor = image_input

        return image_input

    def _get_current_direction(self, vehicle_position):

        # for the current position and orientation try to get the closest one from the waypoints
        closest_id = 0
        min_distance = 100000
        for index in range(len(self._global_plan)):

            waypoint = self._global_plan[index][0]

            computed_distance = distance_vehicle(waypoint, vehicle_position)
            if computed_distance < min_distance:
                min_distance = computed_distance
                closest_id = index

        print ("Closest waypoint ", closest_id, "dist ", min_distance)
        direction = self._global_plan[closest_id][1]

        if direction == RoadOption.LEFT:
            direction = 3.0
        elif direction == RoadOption.RIGHT:
            direction = 4.0
        elif direction == RoadOption.STRAIGHT:
            direction = 5.0
        else:
            direction = 2.0

        return direction

    def _process_model_outputs(self, outputs):
        """
         A bit of heuristics in the control, to eventually make car faster, for instance.
        Returns:

        """
        steer, throttle, brake = outputs[0], outputs[1], outputs[2]
        if brake < 0.05:
            brake = 0.0

        if throttle > brake:
            brake = 0.0

        return steer, throttle, brake

    def _expand_commands(self, topological_plan):
        """ The idea is to make the intersection indications to last longer"""

        # O(2*N) algorithm , probably it is possible to do in O(N) with queues.

        # Get the index where curves start and end
        curves_start_end = []
        inside = False
        start = -1
        current_curve = RoadOption.LANEFOLLOW
        for index in range(len(topological_plan)):

            command = topological_plan[index][1]
            print (command)

            if command != RoadOption.LANEFOLLOW and not inside:
                print ("A curve starts")
                inside = True
                start = index
                current_curve = command

            if command == RoadOption.LANEFOLLOW and inside:
                print ("A curve ends")
                inside = False
                # End now is the index.
                curves_start_end.append([start, index, current_curve])
                if start == -1:
                    raise ValueError("End of curve without start")

                start = -1

        print (curves_start_end)

        for start_end_index_command in curves_start_end:
            start_index = start_end_index_command[0]
            end_index = start_end_index_command[1]
            command = start_end_index_command[2]

            # Add the backwards curves ( Before the begginning)
            for index in range(1, self._expand_command_front + 1):
                changed_index = start_index - index
                if changed_index > 0:
                    topological_plan[changed_index] = (topological_plan[changed_index][0], command)

            # add the onnes after the end
            for index in range(0, self._expand_command_back):
                changed_index = end_index + index
                if changed_index < len(topological_plan):
                    topological_plan[changed_index] = (topological_plan[changed_index][0], command)

        print (topological_plan)

        return topological_plan