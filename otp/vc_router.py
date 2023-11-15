import math
import os
import signal
import uuid
from datetime import datetime

import pymongo.errors

import otp
import otp_builder as builder
from mongo.mongo import get_database


def __create_route_calculation_jobs(db):
    pipeline = [
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

    for doc in result:
        vc_id = doc["vc-id"]
        vc_set_id = doc["vc-set-id"]
        created = datetime.now()

        job = {
            "vc-id": vc_id,
            "vc-set-id": vc_set_id,
            "created": created,
            "status": "pending"
        }

        try:
            jobs_coll.insert_one(job)
        except pymongo.errors.DuplicateKeyError:
            continue  # job was created by other process while iterating


def __route_virtual_commuter(vc, uses_delays):
    # TODO route multiple mode combinations
    origin = vc["origin"]["coordinates"]
    destination = vc["destination"]["coordinates"]
    departure = vc["departure"]

    modes = ["WALK", "TRANSIT"]

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

    return [{
        "route-option-id": str(uuid.uuid4()),
        "origin": vc["origin"],
        "destination": vc["destination"],
        "departure": vc["departure"],
        "modes": modes,
        "itineraries": itineraries
    }]


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


def __iterate_jobs(db, vc_set_id, meta):
    jobs_coll = db["route-calculation-jobs"]
    route_results_coll = db["route-results"]
    route_options_coll = db["route-options"]

    pipeline = [
        {
            "$match": {
                "status": "pending",
                "vc-set-id": vc_set_id
            }
        },
        {
            "$lookup": {
                "from": "virtual-commuters",
                "localField": "vc-id",
                "foreignField": "vc-id",
                "as": "matched_docs"
            }
        },
        {
            "$match": {
                "matched_docs": {
                    "$size": 1
                }
            }
        }
    ]

    use_delays = meta["uses-delay-simulation"]

    jobs_to_calculate = jobs_coll.aggregate(pipeline)

    # by default, we will not stop the process if there is one error, but if there are multiple consecutive errors,
    # we will stop the process
    consecutive_error_number = 0

    for doc in jobs_to_calculate:
        if doc["status"] != "pending":
            continue

        # set status to running
        jobs_coll.update_one({"_id": doc["_id"]}, {"$set": {"status": "running", "started": datetime.now()}})

        print("Running routing algorithm")

        try:
            vc = doc["matched_docs"][0]
            options = __route_virtual_commuter(vc, use_delays)

            if options is None:
                raise Exception("No route found")

            # dump options to route-results collection
            route_results = {
                "vc-id": vc["vc-id"],
                "vc-set-id": vc["vc-set-id"],
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
                "vc-set-id": vc["vc-set-id"],
                "created": datetime.now(),
                "traveller": vc["traveller"],
                "options": [__extract_relevant_data(option) for option in options],
            }

            try:
                route_options_coll.insert_one(route_options)
            except pymongo.errors.DuplicateKeyError:
                if "_id" in route_options:
                    del route_options["_id"]
                route_options_coll.update_one({"vc-id": vc["vc-id"]}, {"$set": route_options})

            # set status to finished
            jobs_coll.update_one({"_id": doc["_id"]}, {"$set": {"status": "done", "finished": datetime.now()}})

            consecutive_error_number = 0
        except Exception as e:
            short_description = "Exception occurred while running routing algorithm: " + e.__class__.__name__ + ": " \
                                + str(e)

            print(short_description)

            # set status to failed
            jobs_coll.update_one({"_id": doc["_id"]},
                                 {"$set": {"status": "error", "error": short_description, "finished": datetime.now()}})

            consecutive_error_number += 1

            if consecutive_error_number >= 5:
                print("Too many consecutive errors, stopping")
                break


def __wait_for_line(process, line_to_wait_for):
    for line in iter(process.stdout.readline, b''):
        print(line, end='')  # Optional: print the line
        if line_to_wait_for in line:
            break


def run(vc_set_id, use_delays=True):
    db = get_database()

    __create_route_calculation_jobs(db)

    vc_set = db["virtual-commuters-sets"].find_one({"vc-set-id": vc_set_id})
    place_resources = db["place-resources"].find_one({"place-id": vc_set["place-id"]})
    pivot_date = vc_set["pivot-date"]

    resources = builder.build_graph(place_resources, pivot_date)

    proc = builder.run_server(resources["graph_file"])

    meta = {
        "otp-version": resources["otp_version"],
        "osm-dataset-link": resources["osm_source"]["source"],
        "osm-dataset-date": resources["osm_source"]["date"],
        "gtfs-dataset-link": resources["gtfs_source"]["source"],
        "gtfs-dataset-date": resources["gtfs_source"]["date"],
        "uses-delay-simulation": use_delays
    }

    if proc is None:
        print("Server not started")
        exit(1)

    try:
        __wait_for_line(proc, "Grizzly server running.")  # that is the last line printed by the server when it is ready
        print("Server started")

        __iterate_jobs(db, vc_set_id, meta)

    finally:
        print("Terminating server...")

        os.kill(proc.pid, signal.CTRL_C_EVENT)  # clean shutdown with CTRL+C

        print("Server terminated")


run("35e58b29-ea03-4b34-b533-05c848b9fb31")