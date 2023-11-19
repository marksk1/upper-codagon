import argparse
import math
import os
import signal
import threading
import time
import uuid
from datetime import datetime, timedelta

import pymongo.errors

import otp
import otp_builder as builder
from mongo.mongo import get_database
import vc_extract


def __reset_jobs(db, sim_id):
    """
    Reset all jobs for the given simulation to pending.
    :param db: the database
    :return:
    """

    print("Resetting jobs for simulation {}".format(sim_id))

    coll = db["route-calculation-jobs"]
    coll.update_many({"sim-id": sim_id},
                     {"$set": {"status": "pending"}, "$unset": {"error": "", "started": "", "finished": ""}})


def __create_route_calculation_jobs(db, sim_id):
    """
    Create route calculation jobs for all virtual commuters of a given simulation that do not have a job yet.
    :param db: the database
    :param sim_id: the simulation id
    :return:
    """
    pipeline = [
        {
            "$match": {
                "sim-id": sim_id
            }
        },
        {
            "$lookup": {
                "from": "route-calculation-jobs",
                "localField": "vc-id",
                "foreignField": "vc-id",
                "as": "matched_docs"
            }
        },
        {
            "$match": {
                "matched_docs": {
                    "$size": 0
                }
            }
        }
    ]

    coll = db["virtual-commuters"]

    result = coll.aggregate(pipeline)
    jobs_coll = db["route-calculation-jobs"]

    print("Creating jobs for simulation {}".format(sim_id))
    for doc in result:
        vc_id = doc["vc-id"]
        sim_id = doc["sim-id"]
        created = datetime.now()

        job = {
            "vc-id": vc_id,
            "sim-id": sim_id,
            "created": created,
            "status": "pending"
        }

        try:
            jobs_coll.insert_one(job)
        except pymongo.errors.DuplicateKeyError:
            continue  # job was created by other process while iterating


def __reset_timed_out_jobs(db):
    """
    Reset jobs that have been running for more than 5 minutes.
    :param db: the database
    :return:
    """
    jobs_coll = db["route-calculation-jobs"]

    print("Resetting timed out jobs")

    jobs_coll.update_many({
        "status": "running",
        "started": {
            "$lt": datetime.now() - timedelta(minutes=5)
        }
    }, {
        "$set": {
            "status": "pending"
        }
    })


def __get_route_option(vc, sim, uses_delays, modes):
    """
    Get a route for a virtual commuter.
    :param vc: The virtual commuter
    :param sim: The simulation
    :param uses_delays: Whether to use delay simulation or not
    :param modes: The modes to use
    :return:
    """
    origin = vc_extract.extract_origin_loc(vc)
    destination = vc_extract.extract_destination_loc(vc)
    departure = vc_extract.extract_departure(vc, sim)

    departure_date = departure.strftime("%Y-%m-%d")
    departure_time = departure.strftime("%H:%M")

    if uses_delays:
        itinerary = otp.get_delayed_route(origin[1], origin[0], destination[1], destination[0], departure_date,
                                          departure_time, False, modes)
        if itinerary is None:
            return None

        itineraries = [itinerary]

    else:
        itineraries = otp.get_route(origin[1], origin[0], destination[1], destination[0], departure_date,
                                    departure_time,
                                    False, modes)

    if itineraries is None or len(itineraries) == 0:
        return None

    return {
        "route-option-id": str(uuid.uuid4()),
        "origin": origin,
        "destination": destination,
        "departure": departure,
        "modes": modes,
        "itineraries": itineraries
    }


def __route_virtual_commuter(vc, sim, uses_delays):
    """
    Route a virtual commuter. It will calculate available mode combinations and then calculate routes for each of them.
    :param vc:
    :param uses_delays:
    :return:
    """
    mode_combinations = [["WALK", "TRANSIT"]]

    if vc_extract.has_motor_vehicle(vc):
        mode_combinations += [["WALK", "CAR"]]

    options = [__get_route_option(vc, sim, uses_delays, modes) for modes in mode_combinations]
    options = [option for option in options if option is not None]

    return options


def __approx_dist(origin, destination):
    """
    Approximate the distance between two points in meters using the Haversine formula.

    :param origin: object with fields lon, lat
    :param destination: object with fields lon, lat
    :return: distance in meters
    """

    # Convert latitude and longitude from degrees to radians
    lon1 = math.radians(origin["lon"])
    lat1 = math.radians(origin["lat"])
    lon2 = math.radians(destination["lon"])
    lat2 = math.radians(destination["lat"])

    # Radius of the Earth in kilometers
    R = 6371.0

    # Difference in coordinates
    dlon = lon2 - lon1
    dlat = lat2 - lat1

    # Haversine formula
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

    # Distance in kilometers
    distance_km = R * c

    # Convert to meters
    distance_meters = distance_km * 1000

    return distance_meters


def __extract_mode_data(leg):
    """
    Extract relevant data from a leg.
    :param leg: The trip leg of an itinerary
    :return: an object with mode, duration (in seconds) and distance (in meters)
    """
    mode = leg["mode"].lower()
    start_time = leg["startTime"]
    if "rtStartTime" in leg:
        start_time = leg["rtStartTime"]
    end_time = leg["endTime"]
    if "rtEndTime" in leg:
        end_time = leg["rtEndTime"]
    duration = (end_time - start_time) / 1000

    origin = leg["from"]
    destination = leg["to"]

    # TODO: distance can be better calculated using steps or shapes
    distance = __approx_dist(origin, destination)

    return {
        "mode": mode,
        "duration": duration,
        "distance": distance
    }


def __extract_relevant_data(route_details):
    """
    Extract relevant data from a route details object.

    :param route_details: Route details. The output of __get_route_option
    :return: An object with route-option-id, route-duration (in s), route-changes, route-delay (in s),
             route-recalculated, modes (array of objects with mode, duration (in s) and distance (in m))
    """
    itinerary = route_details["itineraries"][0]
    start_time = itinerary["startTime"]
    end_time = itinerary["endTime"]
    actual_end_time = end_time
    if "rtEndTime" in itinerary:
        actual_end_time = itinerary["rtEndTime"]

    delay = (actual_end_time - end_time) / 1000
    duration = (actual_end_time - start_time) / 1000

    changes = -1

    for leg in itinerary["legs"]:
        if leg["mode"] == "WALK":
            continue
        changes += 1

    if changes == -1:
        changes = 0

    modes = [__extract_mode_data(leg) for leg in itinerary["legs"]]

    return {
        "route-option-id": route_details["route-option-id"],
        "route-duration": duration,
        "route-changes": changes,
        "route-delay": delay,
        "route-recalculations": itinerary["reCalcCount"],
        "modes": modes
    }


def __no_active_jobs(db, sim_id):
    jobs_coll = db["route-calculation-jobs"]
    return jobs_coll.count_documents({"sim-id": sim_id, "status": "pending"}) == 0


def __process_virtual_commuter(route_results_coll, route_options_coll, vc, sim, use_delays, meta):
    options = __route_virtual_commuter(vc, sim, use_delays)

    if options is None or len(options) == 0:
        raise Exception("No route found")

    # dump options to route-results collection
    route_results = {
        "vc-id": vc["vc-id"],
        "sim-id": vc["sim-id"],
        "created": datetime.now(),
        "options": options,
        "meta": meta
    }

    try:
        route_results_coll.insert_one(route_results)
    except pymongo.errors.DuplicateKeyError:
        if "_id" in route_results:
            del route_results["_id"]
        route_results_coll.update_one({"vc-id": vc["vc-id"]}, {"$set": route_results})

    # extract relevant data for decision making
    route_options = {
        "vc-id": vc["vc-id"],
        "sim-id": vc["sim-id"],
        "created": datetime.now(),
        "traveller": vc_extract.extract_traveller(vc),
        "options": [__extract_relevant_data(option) for option in options],
    }

    try:
        route_options_coll.insert_one(route_options)
    except pymongo.errors.DuplicateKeyError:
        if "_id" in route_options:
            del route_options["_id"]
        route_options_coll.update_one({"vc-id": vc["vc-id"]}, {"$set": route_options})


def __iterate_jobs(db, sim, meta, debug=False, progress_fac=1):
    jobs_coll = db["route-calculation-jobs"]
    vc_coll = db["virtual-commuters"]
    route_results_coll = db["route-results"]
    route_options_coll = db["route-options"]

    sim_id = sim["sim-id"]

    # get total number of jobs
    total_jobs = jobs_coll.count_documents({"sim-id": sim_id, "status": "pending"})

    use_delays = meta["uses-delay-simulation"]

    # by default, we will not stop the process if there is one error, but if there are multiple consecutive errors,
    # we will stop the process
    consecutive_error_number = 0

    print("Running routing algorithm")

    job_key = 0
    last_print = 0

    while True:
        job = jobs_coll.find_one_and_update({
            "status": "pending",
            "sim-id": sim_id
        }, {
            "$set": {
                "status": "running",
                "started": datetime.now()
            }
        })

        if job is None:
            break

        job_key += 1
        percentage = job_key / total_jobs * 100
        current_time = time.time()
        if debug and current_time - last_print > 1:
            print("Progress: ~{:.2f}% {:}".format(percentage * progress_fac, job["vc-id"]))
            last_print = current_time

        try:
            vc = vc_coll.find_one({"vc-id": job["vc-id"]})

            should_route = vc_extract.should_route(vc)

            if should_route:
                __process_virtual_commuter(route_results_coll, route_options_coll, vc, sim, use_delays, meta)

            # set status to finished
            jobs_coll.update_one({"_id": job["_id"]}, {"$set": {"status": "done", "finished": datetime.now()}})
        except Exception as e:
            short_description = "Exception occurred while running routing algorithm: " + e.__class__.__name__ + ": " \
                                + str(e)

            print(short_description)

            # set status to failed
            jobs_coll.update_one({"_id": job["_id"]},
                                 {"$set": {"status": "error", "error": short_description, "finished": datetime.now()}})

            consecutive_error_number += 1

            if consecutive_error_number >= 5:
                print("Too many consecutive errors, stopping")
                break


def __spawn_job_pull_threads(db, sim, meta, num_threads=4):
    threads = []

    for i in range(num_threads):
        t = threading.Thread(target=__iterate_jobs, args=(db, sim, meta, i == 0, num_threads))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()


def __wait_for_line(process, line_to_wait_for):
    """
    Wait for a specific line to appear in the output of a process

    :param process: the process
    :param line_to_wait_for: the line to wait for
    :return:
    """
    for line in iter(process.stdout.readline, b''):
        print(line, end='')  # Optional: print the line
        if line_to_wait_for in line:
            break


def run(sim_id, use_delays=True, force_graph_rebuild=False, graph_build_memory=4, server_memory=4, num_threads=4,
        reset_jobs=False):
    """
    Run the routing algorithm for a virtual commuter set. It will spawn a new OTP process and run the routing algorithm
    for all open jobs in the database. It will also update the database with the results of the routing algorithm.
    :param sim_id: The virtual commuter set id
    :param use_delays: Whether to use delays or not
    :param force_graph_rebuild: Whether to force a rebuild of the graph or not
    :param graph_build_memory: The amount of memory to use for the graph build process
    :param server_memory: The amount of memory to use for the OTP server
    :param num_threads: The number of threads to use for sending route requests to the server
    :param reset_jobs: Whether to reset all jobs to pending or not
    :return:
    """
    db = get_database()

    if reset_jobs:
        __reset_jobs(db, sim_id)

    __create_route_calculation_jobs(db, sim_id)
    __reset_timed_out_jobs(db)

    if __no_active_jobs(db, sim_id):
        print("No active jobs, stopping")
        return

    sim = db["simulations"].find_one({"sim-id": sim_id})
    place_resources = db["place-resources"].find_one({"place-id": sim["place-id"]})
    pivot_date = sim["pivot-date"]

    print("Building graph")
    resources = builder.build_graph(place_resources, pivot_date, force_graph_rebuild, graph_build_memory)

    proc = builder.run_server(resources["graph_file"], server_memory)

    meta = {
        "otp-version": resources["otp_version"],
        "osm-dataset-link": resources["osm_source"]["source"],
        "osm-dataset-date": resources["osm_source"]["date"],
        "gtfs": [{"source": source["source"], "date": source["date"], "provider": source["provider"]} for source in
                 resources["gtfs_sources"]],
        "uses-delay-simulation": use_delays
    }

    if proc is None:
        print("Server not started")
        exit(1)

    try:
        print("Starting up server...")
        __wait_for_line(proc, "Grizzly server running.")  # that is the last line printed by the server when it is ready
        print("Server started")

        t = datetime.now()

        __spawn_job_pull_threads(db, sim, meta, num_threads)

        print("Finished routing algorithm in " + str(datetime.now() - t))

    finally:
        print("Terminating server...")

        try:
            os.kill(proc.pid, signal.CTRL_C_EVENT)  # clean shutdown with CTRL+C

            proc.wait()
        except KeyboardInterrupt:
            pass

        print("Server terminated")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Run the routing algorithm for a virtual commuter set')
    parser.add_argument('sim_id', type=str, help='Simulation id')
    parser.add_argument('--no-delays', dest='no_delays', action='store_true', help='Whether to use delays or not')
    parser.add_argument('--force-graph-rebuild', dest='force_graph_rebuild', action='store_true', help='Whether to '
                                                                                                       'force a rebuild of the graph or not')
    parser.add_argument('--graph-build-memory', dest='graph_build_memory', type=int, default=4, help='The amount of '
                                                                                                     'memory to use for the graph build process (in GB)')
    parser.add_argument('--server-memory', dest='server_memory', type=int, default=4, help='The amount of memory to '
                                                                                           'use for the OTP server (in GB)')
    parser.add_argument('--num-threads', dest='num_threads', type=int, default=4, help='The number of threads to use '
                                                                                       'for sending route requests to the server')
    parser.add_argument('--reset-jobs', dest='reset_jobs', action='store_true', help='Whether to reset all jobs for '
                                                                                     'this simulation')

    args = parser.parse_args()

    run(args.sim_id, not args.no_delays, args.force_graph_rebuild, args.graph_build_memory, args.server_memory,
        args.num_threads,
        args.reset_jobs)
