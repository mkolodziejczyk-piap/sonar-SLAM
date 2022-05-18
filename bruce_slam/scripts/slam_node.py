#!/usr/bin/env python
from typing import Any
import matplotlib

matplotlib.use("Agg")

import threading
import matplotlib.pyplot as plt
from scipy.spatial import cKDTree as KDTree
from scipy.spatial.transform import Rotation

import tf
import rospy
import cv_bridge
from message_filters import ApproximateTimeSynchronizer
from message_filters import Cache, Subscriber
from visualization_msgs.msg import Marker
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import Odometry, Path, OccupancyGrid
from sensor_msgs.msg import PointCloud2
from gazebo_msgs.msg import LinkState 
from gazebo_msgs.msg import ModelStates, LinkStates
from gazebo_msgs.srv import GetLinkState
from geometry_msgs.msg import Point as point_msg
from visualization_msgs.msg import Marker

# For exploration services
from bruce_msgs.srv import PredictSLAMUpdate, PredictSLAMUpdateResponse
from bruce_msgs.msg import ISAM2Update
from bruce_msgs.srv import GetOccupancyMap, GetOccupancyMapRequest

from bruce_slam.utils.io import *
from bruce_slam.utils.conversions import *
from bruce_slam.utils.visualization import *
from bruce_slam.slam import SLAM, Keyframe
from bruce_slam import pcl
from sonar_oculus.msg import OculusPing



class SLAMNode(SLAM):
    '''Class to handle the SLAM problem
    '''
    def __init__(self):
        super(SLAMNode, self).__init__()

        #self.enable_slam = True #always set to true

        self.pz_samples = 30
        self.pz_detection_rate = 0.5

        # the threading lock
        self.lock = threading.RLock()

    def init_node(self, ns="~")->None:
        """Configures the SLAM node

        Args:
            ns (str, optional): The namespace of the node. Defaults to "~".
        """

        #keyframe paramters, how often to add them
        self.keyframe_duration = rospy.get_param(ns + "keyframe_duration")
        self.keyframe_duration = rospy.Duration(self.keyframe_duration)
        self.keyframe_translation = rospy.get_param(ns + "keyframe_translation")
        self.keyframe_rotation = rospy.get_param(ns + "keyframe_rotation")

        #SLAM paramter, are we using SLAM or just dead reckoning
        self.enable_slam = rospy.get_param(ns + "enable_slam")
        print("SLAM STATUS: ", self.enable_slam)

        #noise models
        self.prior_sigmas = rospy.get_param(ns + "prior_sigmas")
        self.odom_sigmas = rospy.get_param(ns + "odom_sigmas")
        self.icp_odom_sigmas = rospy.get_param(ns + "icp_odom_sigmas")

        #resultion for map downsampling
        self.point_resolution = rospy.get_param(ns + "point_resolution")

        #sequential scan matching parameters (SSM)
        self.ssm_params.enable = rospy.get_param(ns + "ssm/enable")
        self.ssm_params.min_points = rospy.get_param(ns + "ssm/min_points")
        self.ssm_params.max_translation = rospy.get_param(ns + "ssm/max_translation")
        self.ssm_params.max_rotation = rospy.get_param(ns + "ssm/max_rotation")
        self.ssm_params.target_frames = rospy.get_param(ns + "ssm/target_frames")
        print("SSM: ", self.ssm_params.enable)

        #non sequential scan matching parameters (NSSM) aka loop closures
        self.nssm_params.enable = rospy.get_param(ns + "nssm/enable")
        self.nssm_params.min_st_sep = rospy.get_param(ns + "nssm/min_st_sep")
        self.nssm_params.min_points = rospy.get_param(ns + "nssm/min_points")
        self.nssm_params.max_translation = rospy.get_param(ns + "nssm/max_translation")
        self.nssm_params.max_rotation = rospy.get_param(ns + "nssm/max_rotation")
        self.nssm_params.source_frames = rospy.get_param(ns + "nssm/source_frames")
        self.nssm_params.cov_samples = rospy.get_param(ns + "nssm/cov_samples")
        print("NSSM: ", self.nssm_params.enable)

        #paramters for predicting measurnemnts, used by EM exploration
        self.pz_samples = rospy.get_param(ns + "pz_samples")
        self.pz_detection_rate = rospy.get_param(ns + "pz_detection_rate")

        #pairwise consistency maximization parameters for loop closure 
        #outliar rejection
        self.pcm_queue_size = rospy.get_param(ns + "pcm_queue_size")
        self.min_pcm = rospy.get_param(ns + "min_pcm")

        #mak delay between an incoming point cloud and dead reckoning
        self.feature_odom_sync_max_delay = 0.5

        #define the subsrcibing topics
        self.feature_sub = Subscriber(SONAR_FEATURE_TOPIC, PointCloud2)
        self.odom_sub = Subscriber(LOCALIZATION_ODOM_TOPIC, Odometry)

        #define the sync policy
        self.time_sync = ApproximateTimeSynchronizer(
            [self.feature_sub, self.odom_sub], 20, 
            self.feature_odom_sync_max_delay, allow_headerless = False)

        #register the callback in the sync policy
        self.time_sync.registerCallback(self.SLAM_callback)

        #pose publisher
        self.pose_pub = rospy.Publisher(
            SLAM_POSE_TOPIC, PoseWithCovarianceStamped, queue_size=10)

        #dead reckoning topic
        self.odom_pub = rospy.Publisher(SLAM_ODOM_TOPIC, Odometry, queue_size=10)

        #SLAM trajectory topic
        self.traj_pub = rospy.Publisher(
            SLAM_TRAJ_TOPIC, PointCloud2, queue_size=1, latch=True)

        #constraints between poses
        self.constraint_pub = rospy.Publisher(
            SLAM_CONSTRAINT_TOPIC, Marker, queue_size=1, latch=True)

        #point cloud publisher topic
        self.cloud_pub = rospy.Publisher(
            SLAM_CLOUD_TOPIC, PointCloud2, queue_size=1, latch=True)

        #publish the entire GTSAM instance
        self.slam_update_pub = rospy.Publisher(
            SLAM_ISAM2_TOPIC, ISAM2Update, queue_size=5, latch=True)

        #tf broadcaster to show pose
        self.tf = tf.TransformBroadcaster()

        #cv bridge object
        self.CVbridge = cv_bridge.CvBridge()

        #get the ICP configuration from the yaml fukle
        icp_config = rospy.get_param(ns + "icp_config")
        #icp_ssm_config = rospy.get_param(ns + "icp_ssm_config")
        self.icp.loadFromYaml(icp_config)
        #self.icp_ssm.loadFromYaml(icp_ssm_config)

        #call the configure function
        self.configure()
        loginfo("SLAM node is initialized")

    @add_lock
    def sonar_callback(self, ping:OculusPing)->None:
        """Subscribe once to configure Oculus property.
        Assume sonar configuration doesn't change much.

        Args:
            ping (OculusPing): The sonar message. 
        """
        self.oculus.configure(ping)
        self.sonar_sub.unregister()

    @add_lock
    def SLAM_callback(self, feature_msg:PointCloud2, odom_msg:Odometry)->None:
        """SLAM call back. Subscibes to the feature msg point cloud and odom msg
            Handles the whole SLAM system and publishes map, poses and constraints

        Args:
            feature_msg (PointCloud2): the incoming sonar point cloud
            odom_msg (Odometry): the incoming DVL/IMU state estimate
        """

        #aquire the lock 
        self.lock.acquire()

        #get rostime from the point cloud
        time = feature_msg.header.stamp

        #get the dead reckoning pose from the odom msg, GTSAM pose object
        dr_pose3 = r2g(odom_msg.pose.pose)

        #init a new key frame
        frame = Keyframe(False, time, dr_pose3)

        #convert the point cloud message to a numpy array of 2D
        points = ros_numpy.point_cloud2.pointcloud2_to_xyz_array(feature_msg)
        points = np.c_[points[:,0] , -1 *  points[:,2]]

        # In case feature extraction is skipped in this frame
        if len(points) and np.isnan(points[0, 0]):
            frame.status = False
        else:
            frame.status = self.is_keyframe(frame)

        #set the frames twist
        frame.twist = odom_msg.twist.twist

        #update the keyframe with pose information from dead reckoning
        if self.keyframes:
            dr_odom = self.current_keyframe.dr_pose.between(frame.dr_pose)
            pose = self.current_keyframe.pose.compose(dr_odom)
            frame.update(pose)


        #check frame staus, are we actually adding a keyframe? This is determined based on distance 
        #traveled according to dead reckoning
        if frame.status:

            #add the point cloud to the frame
            frame.points = points

            #perform seqential scan matching
            #if this is the first frame do not
            if not self.keyframes:
                self.add_prior(frame)
            else:
                self.add_sequential_scan_matching(frame)

            #update the factor graph with the new frame
            self.update_factor_graph(frame)

            #if loop closures are enabled
            #nonsequential scan matching is True (a loop closure occured) update graph again
            if self.nssm_params.enable  and self.add_nonsequential_scan_matching():
                self.update_factor_graph()
            
        #update current time step and publish the topics
        self.current_frame = frame
        self.publish_all()
        self.lock.release()

    def publish_all(self)->None:
        """Publish to all ouput topics
            trajectory, contraints, point cloud and the full GTSAM instance
        """
        if not self.keyframes:
            return

        self.publish_pose()
        if self.current_frame.status:
            self.publish_trajectory()
            self.publish_constraint()
            self.publish_point_cloud()
            self.publish_slam_update()

    def publish_pose(self)->None:
        """Append dead reckoning from Localization to SLAM estimate to achieve realtime TF.
        """

        #define a pose with covariance message 
        pose_msg = PoseWithCovarianceStamped()
        pose_msg.header.stamp = self.current_frame.time
        pose_msg.header.frame_id = "map"
        pose_msg.pose.pose = g2r(self.current_frame.pose3)

        cov = 1e-4 * np.identity(6, np.float32)
        # FIXME Use cov in current_frame
        cov[np.ix_((0, 1, 5), (0, 1, 5))] = self.current_keyframe.transf_cov
        pose_msg.pose.covariance = cov.ravel().tolist()
        self.pose_pub.publish(pose_msg)

        o2m = self.current_frame.pose3.compose(self.current_frame.dr_pose3.inverse())
        o2m = g2r(o2m)
        p = o2m.position
        q = o2m.orientation
        self.tf.sendTransform(
            (p.x, p.y, p.z),
            [q.x, q.y, q.z, q.w],
            self.current_frame.time,
            "odom",
            "map",
        )

        odom_msg = Odometry()
        odom_msg.header = pose_msg.header
        odom_msg.pose.pose = pose_msg.pose.pose
        odom_msg.child_frame_id = "base_link"
        odom_msg.twist.twist = self.current_frame.twist
        self.odom_pub.publish(odom_msg)

    def publish_constraint(self)->None:
        """Publish constraints between poses in the factor graph,
        either sequential or non-sequential.
        """

        #define a list of all the constraints
        links = []

        #iterate over all the keframes
        for x, kf in enumerate(self.keyframes[1:], 1):

            #append each SSM factor in green
            p1 = self.keyframes[x - 1].pose3.x(), self.keyframes[x - 1].pose3.y(), self.keyframes[x - 1].dr_pose3.z()
            p2 = self.keyframes[x].pose3.x(), self.keyframes[x].pose3.y(), self.keyframes[x].dr_pose3.z()
            links.append((p1, p2, "green"))

            #loop over all loop closures in this keyframe and append them in red
            for k, _ in self.keyframes[x].constraints:
                p0 = self.keyframes[k].pose3.x(), self.keyframes[k].pose3.y(), self.keyframes[k].dr_pose3.z()
                links.append((p0, p2, "red"))

        #if nothing, do nothing
        if links:

            #conver this list to a series of multi-colored lines and publish
            link_msg = ros_constraints(links)
            link_msg.header.stamp = self.current_keyframe.time
            self.constraint_pub.publish(link_msg)


    def publish_trajectory(self)->None:
        """Publish 3D trajectory as point cloud in [x, y, z, roll, pitch, yaw, index] format.
        """

        #get all the poses from each keyframe
        poses = np.array([g2n(kf.pose3) for kf in self.keyframes])

        #convert to a ros color line
        traj_msg = ros_colorline_trajectory(poses)
        traj_msg.header.stamp = self.current_keyframe.time
        traj_msg.header.frame_id = "map"
        self.traj_pub.publish(traj_msg)

    def publish_point_cloud(self)->None:
        """Publish downsampled 3D point cloud with z = 0.
        The last column represents keyframe index at which the point is observed.
        """

        #define an empty array
        all_points = [np.zeros((0, 2), np.float32)]

        #list of keyframe ids
        all_keys = []

        #loop over all the keyframes, register 
        #the point cloud to the orign based on the SLAM estinmate
        for key in range(len(self.keyframes)):

            #parse the pose
            pose = self.keyframes[key].pose

            #get the resgistered point cloud
            transf_points = self.keyframes[key].transf_points

            #append
            all_points.append(transf_points)
            all_keys.append(key * np.ones((len(transf_points), 1)))

        all_points = np.concatenate(all_points)
        all_keys = np.concatenate(all_keys)

        #use PCL to downsample this point cloud
        sampled_points, sampled_keys = pcl.downsample(
            all_points, all_keys, self.point_resolution
        )

        #parse the downsampled cloud into the ros xyzi format
        sampled_xyzi = np.c_[sampled_points, np.zeros_like(sampled_keys), sampled_keys]
        
        #if there are no points return and do nothing
        if len(sampled_xyzi) == 0:
            return

        #convert the point cloud to a ros message and publish
        cloud_msg = n2r(sampled_xyzi, "PointCloudXYZI")
        cloud_msg.header.stamp = self.current_keyframe.time
        cloud_msg.header.frame_id = "map"
        self.cloud_pub.publish(cloud_msg)

    def publish_slam_update(self):
        """
        Publish the entire ISAM2 instance for exploration server.
        So BayesTree isn't built from scratch.
        """
        '''update_msg = ISAM2Update()
        update_msg.header.stamp = self.current_keyframe.time
        update_msg.key = self.current_key - 1
        update_msg.isam2 = gtsam.serializeISAM2(self.isam)
        self.slam_update_pub.publish(update_msg)'''
        pass

def offline(args:Any)->None:
    """run the SLAM system offline

    Args:
        args (Any): the arguments to run the system
    """

    # pull in the extra imports required
    from rosgraph_msgs.msg import Clock
    from dead_reckoning_node import DeadReckoningNode
    from feature_extraction_node import FeatureExtraction
    from gyro_node import GyroFilter
    from mapping_node import MappingNode
    from bruce_slam.utils import io

    # set some params
    io.offline = True
    node.save_fig = False
    node.save_data = False

    # instaciate the nodes required
    dead_reckoning_node = DeadReckoningNode()
    dead_reckoning_node.init_node(SLAM_NS + "localization/")
    feature_extraction_node = FeatureExtraction()
    feature_extraction_node.init_node(SLAM_NS + "feature_extraction/")
    gyro_node = GyroFilter()
    gyro_node.init_node(SLAM_NS + "gyro/")
    """mp_node = MappingNode()
    mp_node.init_node(SLAM_NS + "mapping/")"""
    clock_pub = rospy.Publisher("/clock", Clock, queue_size=100)

    # loop over the entire rosbag
    for topic, msg in read_bag(args.file, args.start, args.duration, progress=True):
        while not rospy.is_shutdown():
            if callback_lock_event.wait(1.0):
                break

        if rospy.is_shutdown():
            break

        if topic == IMU_TOPIC or topic == IMU_TOPIC_MK_II:
            dead_reckoning_node.imu_sub.callback(msg)
        elif topic == DVL_TOPIC:
            dead_reckoning_node.dvl_sub.callback(msg)
        elif topic == DEPTH_TOPIC:
            dead_reckoning_node.depth_sub.callback(msg)
        elif topic == SONAR_TOPIC:
            feature_extraction_node.sonar_sub.callback(msg)
        elif topic == GYRO_TOPIC:
            gyro_node.gyro_sub.callback(msg)

        # use the IMU to drive the clock
        if topic == IMU_TOPIC or topic == IMU_TOPIC_MK_II:

            clock_pub.publish(Clock(msg.header.stamp))

            # Publish map to world so we can visualize all in a z-down frame in rviz.
            node.tf.sendTransform((0, 0, 0), [1, 0, 0, 0], msg.header.stamp, "map", "world")
    

if __name__ == "__main__":

    #init the node
    rospy.init_node("slam", log_level=rospy.INFO)

    #call the class constructor
    node = SLAMNode()
    node.init_node()

    #parse and start
    args, _ = common_parser().parse_known_args()

    if not args.file:
        loginfo("Start online slam...")
        rospy.spin()
    else:
        loginfo("Start offline slam...")
        offline(args)
