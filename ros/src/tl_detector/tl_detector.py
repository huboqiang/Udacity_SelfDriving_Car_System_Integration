#!/usr/bin/env python
import rospy
from std_msgs.msg import Int32
from geometry_msgs.msg import PoseStamped, Pose
from styx_msgs.msg import TrafficLightArray, TrafficLight
from styx_msgs.msg import Lane
from sensor_msgs.msg import Image
from cv_bridge import CvBridge
from light_classification.tl_classifier import TLClassifier
import tf
import cv2
import yaml
import math
import sys

STATE_COUNT_THRESHOLD = 3

class TLDetector(object):
    def __init__(self):
        rospy.init_node('tl_detector')

        self.pose = None
        self.waypoints = None
        self.camera_image = None
        self.lights = []

        sub1 = rospy.Subscriber('/current_pose', PoseStamped, self.pose_cb)
        sub2 = rospy.Subscriber('/base_waypoints', Lane, self.waypoints_cb)

        '''
        /vehicle/traffic_lights provides you with the location of the traffic light in 3D map space and 
        helps you acquire an accurate ground truth data source for the traffic light
        classifier by sending the current color state of all traffic lights in the
        simulator. When testing on the vehicle, the color state will not be available. You'll need to
        rely on the position of the light and the camera image to predict it.
        '''
        sub3 = rospy.Subscriber('/vehicle/traffic_lights', TrafficLightArray, self.traffic_cb)
        sub6 = rospy.Subscriber('/image_color', Image, self.image_cb)

        config_string = rospy.get_param("/traffic_light_config")
        self.config = yaml.load(config_string)

        self.upcoming_red_light_pub = rospy.Publisher('/traffic_waypoint', Int32, queue_size=1)

        self.bridge = CvBridge()
        self.light_classifier = TLClassifier()
        self.listener = tf.TransformListener()

        self.state = TrafficLight.UNKNOWN
        self.last_state = TrafficLight.UNKNOWN
        self.last_wp = -1
        self.state_count = 0

        rospy.spin()

    def pose_cb(self, msg):
        self.pose = msg

    def waypoints_cb(self, waypoints):
        self.waypoints = waypoints

    def traffic_cb(self, msg):
        self.lights = msg.lights

    def image_cb(self, msg):
        """Identifies red lights in the incoming camera image and publishes the index
            of the waypoint closest to the red light to /traffic_waypoint

        Args:
            msg (Image): image from car-mounted camera

        """
        self.has_image = True
        self.camera_image = msg
        light_wp, state = self.process_traffic_lights()

        '''
        Publish upcoming red lights at camera frequency.
        Each predicted state has to occur `STATE_COUNT_THRESHOLD` number
        of times till we start using it. Otherwise the previous stable state is
        used.
        '''
        if self.state != state:
            self.state_count = 0
            self.state = state
        elif self.state_count >= STATE_COUNT_THRESHOLD:
            self.last_state = self.state
            light_wp = light_wp if state == TrafficLight.RED else -1
            self.last_wp = light_wp
            self.upcoming_red_light_pub.publish(Int32(light_wp))
        else:
            self.upcoming_red_light_pub.publish(Int32(self.last_wp))
        self.state_count += 1

    def get_closest_waypoint(self, pose):
        """Identifies the closest path waypoint to the given position
            https://en.wikipedia.org/wiki/Closest_pair_of_points_problem
        Args:
            pose (Pose): position to match a waypoint to

        Returns:
            int: index of the closest waypoint in self.waypoints

        """
        # Min distance
        minDist_idx = -1
        minDist_val = sys.float_info.max
        
        # Loop through all waypoints
        for idx, wp in enumerate(self.waypoints):
            dis = lambda a, b: math.sqrt((a.x-b.x)**2 + (a.y-b.y)**2 + (a.z-b.z)**2)
            val = dis(wp.pose.pose.position, pose.position)
            if val < minDist_val:
                minDist_val = val
                minDist_idx = idx    
        
        return minDist_idx


    def project_to_image_plane(self, point_in_world):
        """Project point from 3D world coordinates to 2D camera image location

        Args:
            point_in_world (Point): 3D location of a point in the world

        Returns:
            x (int): x coordinate of target point in image
            y (int): y coordinate of target point in image

        """

        # fx = self.config['camera_info']['focal_length_x']
        # fy = self.config['camera_info']['focal_length_y']
	fx = 2650
	fy = 2250
        image_width = self.config['camera_info']['image_width']
        image_height = self.config['camera_info']['image_height']

        # get transform between pose of camera and world frame
        trans = None
        try:
            now = rospy.Time.now()
            self.listener.waitForTransform("/base_link",
                  "/world", now, rospy.Duration(1.0))
            (trans, rot) = self.listener.lookupTransform("/base_link",
                  "/world", now)

        except (tf.Exception, tf.LookupException, tf.ConnectivityException):
            rospy.logerr("Failed to find camera to map transform")

        # Calculate 2D position of light in image
        euler = tf.transformations.euler_from_quaternion(rot)
        sinyaw = math.sin(euler[2])
        cosyaw = math.cos(euler[2])
        
        x = point_in_world.x * cosyaw - point_in_world.y * sinyaw + trans[0]
        y = point_in_world.x * sinyaw + point_in_world.y * cosyaw + trans[1]
        z = point_in_world.z + trans[2]
        
        if x != 0:
            u = int((- y / x) * fx + image_width / 2)
            v = int((- z / x) * fy + image_height / 2)
        else:
            u = 0
            v = 0

        return (u, v)

    def get_light_state(self, light):
        """Determines the current color of the traffic light

        Args:
            light (TrafficLight): light to classify

        Returns:
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        if(not self.has_image):
            self.prev_light_loc = None
            return False

        cv_image = self.bridge.imgmsg_to_cv2(self.camera_image, "bgr8")

        x, y = self.project_to_image_plane(light.pose.pose.position)

        img_width = cv_image.shape[1]
        img_height = cv_image.shape[0]
        vertical_crop_size = 25
        horizontal_crop_size = 5

	#Check if x,y are inside image
	if x < 0 or y < 0 or x > img_width or y > img_height:
	    return TrafficLight.UNKNOWN
	else:
            #Crop traffic light from image and resize it to (50,50)
            left = x - horizontal_crop_size if (x-horizontal_crop_size) > 0 else 0
            right = x + horizontal_crop_size if (x+horizontal_crop_size) < img_width else img_width
            bottom = y + vertical_crop_size if (y+vertical_crop_size) < img_height else img_height
            top = y - vertical_crop_size if (y-vertical_crop_size) > 0 else 0
            
            cropped_image = cv_image[top:bottom,left:right]

            #Get classification
            return self.light_classifier.get_classification(cropped_image)
        
    def process_traffic_lights(self):
        """Finds closest visible traffic light, if one exists, and determines its
            location and color

        Returns:
            int: index of waypoint closes to the upcoming traffic light (-1 if none exists)
            int: ID of traffic light color (specified in styx_msgs/TrafficLight)

        """
        light = None
        light_positions = self.config['light_positions']
        if(self.pose):
            car_position = self.get_closest_waypoint(self.pose.pose)

        # Find the closest visible traffic light (if one exists)
	minDist_idx = -1
        minDist_val = sys.float_info.max
        
        # Loop through all stop line positions
        for idx, stop_line in enumerate(stop_line_positions):
            dis = lambda a, b: math.sqrt((a.x-b.x)**2 + (a.y-b.y)**2)
            val = dis(stop_line.pose.pose.position, car_position.position)
            if val < minDist_val:
                minDist_val = val
                minDist_idx = idx
        
        light = stop_line_positions[minDist_idx]

        if light:
            state = self.get_light_state(light)
            return light_wp, state
        self.waypoints = None
        return -1, TrafficLight.UNKNOWN

if __name__ == '__main__':
    try:
        TLDetector()
    except rospy.ROSInterruptException:
        rospy.logerr('Could not start traffic node.')