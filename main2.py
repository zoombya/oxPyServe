import asyncio
import websockets
from json import loads, dumps
import uuid
import os
from os import makedirs
from os.path import exists, join
from shutil import rmtree
from multiprocessing import Process, Queue
import oxpy
from time import sleep
import numpy as np


# start with a clean slate
rmtree("./sims")
makedirs("./sims")

defaults = {
    "conf_file"  : "conf_file.dat",
    "topology"  : "top_file.top",
    "trajectory_file"  : "/dev/null",
    "energy_file"  : "energy.dat",
    "lastconf_file"  : "last_conf.dat",
    "max_io"  : "10000",
    "bussi_tau" : "1",
    "seq_dep_file":"../../utils/oxDNA2_sequence_dependent_parameters.txt",
    "log_file" : "logfile",
}


def clean_up(path):
    os.chdir("../../")
    print(f"cleaning up {path}")
    rmtree(path)


def conf_to_string(flat_conf,box):
    lines = []
    a = lines.append
    a("t = 0")
    a(f"b = {box[0]} {box[1]} {box[2]}")
    a("E = 0 0 0")

    # copy the positions
    positions = np.array(flat_conf.positions)
    a1s = np.array(flat_conf.a1s)
    a3s = np.array(flat_conf.a3s)

    # for some reason we have to bring the positions back into the box
    positions = positions - np.round( positions / box ,0) * box

    for i in range(len(positions)):
        a(f"{positions[i][0]} {positions[i][1]} {positions[i][2]} {a1s[i][0]} {a1s[i][1]} {a1s[i][2]} {a3s[i][0]} {a3s[i][1]} {a3s[i][2]}")
    return "\n".join(lines)

def dump_msg_to_file(path, msg):
    with open(path, "w") as f:
        f.write(msg)

def get_log_str(manager,N):
    current_step  = manager.config_info().current_step 
    system_energy = manager.system_energy() / N 
   
    return f"t: {current_step}\tE: {system_energy:.6f}"

def child_process(path, in_queue, out_queue):
    # wait for a message from the parent
    id = in_queue.get()
    print(f"(-) I am {id}")
    # create a directory for the simulation
    makedirs(path)
    os.chdir(path)
    # this entire game is in fact an oxDNA simulation
    # wiht all it's bells and whistles
    pause = False
    with oxpy.Context():
        # so we need a manager
        manager = None
        box = [0,0,0]
        N = 0
        print_conf_interval = 10
        # update the settings with the defaults
        while True:
            if in_queue.empty():
                # we do simulation work, if the manager exists
                if manager is not None and not pause:
                    manager.run(print_conf_interval)
                    # now we want to pass the conf back onto the websocket
                    conf = manager.config_info().flattened_conf
                    # push the conf to the websocket read queue
                    out_queue.put({
                        'console_log': get_log_str(manager,N),
                        'dat_file'   : conf_to_string(conf, box) 
                    })              
                continue
            else:
                message = in_queue.get()
                print(f"(-) {id} received a message")
                if message == "abort":
                    # this will ensure nothing is computed                    
                    return 
                else:
                    # message object
                    object = loads(message)
                    if manager is None:
                        # if our object has the following keys if it's a conf 'top_file', 'dat_file', 'settings'
                        # we need to setup a new simulation
                        if 'top_file' in object.keys() and 'dat_file' in object.keys() and 'settings' in object.keys():
                            # write the top file
                            dump_msg_to_file(f"./top_file.top",  object['top_file'])
                            # write the dat file
                            dump_msg_to_file(f"./conf_file.dat", object['dat_file'])
                            
                            # update the settings with the defaults
                            object['settings'].update(defaults)
                            # write the input file
                            with open(f"./input", "w") as f:
                                for k,v in object['settings'].items():
                                    f.write(f"{k} = {v}\n")

                            # now we try a simulation
                            manager = oxpy.OxpyManager("input")
                            box = manager.config_info().box.box_sides
                            N = manager.config_info().N()
                            
                            # can we now switch the path back to the one the program was started in?
                            os.chdir("../../")


                            # save the print_conf_interval
                            print_conf_interval = int(object["settings"]["print_conf_interval"])
                        else:
                            print(f"(-) {id} received a message that I don't understand YET.")
                            print(message)
                            continue
                    else:
                        pause = False
                        # and do whatever we need to do in message
                


async def read_from_websocket(id, websocket, in_queue):
    # if we have something in the socket, we pass it through to the child process
    while True:
        # connection is closed
        if websocket.closed:
            # clean up the directory if it's still there
            path = f"./sims/{id}/"
            if exists(path):
                print(  os.getcwd() )
                clean_up(path)
                break
        if len(websocket.messages) == 0:
            # no messages to handle so let the event loop do other stuff
            await asyncio.sleep(0)
            continue
        else:
            message = await websocket.recv()
            print(f"Received message from websocket.")
            in_queue.put(message)


async def read_from_child(id, websocket, out_queue):
    while True:
        if out_queue.empty():
            # no messages to handle so let the event loop do other stuff
            await asyncio.sleep(0)
            continue
        message = out_queue.get()
        print(f"Received message from child: {id}")
        await websocket.send(dumps(message))


async def handle_connection(websocket):
    unique_id = uuid.uuid4()
    print(f"New connection from {unique_id}")
    #we generate also the path for the simulation
    path = f"./sims/{unique_id}"

    # per connection, we need to create a new process
    # and a new queue
    in_queue  = Queue()
    out_queue = Queue()
    
    #we generate also the path for the simulation
    path = f"./sims/{unique_id}"

    # start the child process to handle the simulation
    p = Process(target=child_process, args=(path, in_queue, out_queue))
    p.start()

    # send the unique id to the child process
    in_queue.put(unique_id)

    # deifne a task for reading from the websocket passing the message throught to the child process
    websocket_task = asyncio.create_task(read_from_websocket(unique_id, websocket, in_queue))
    # define one task for reading from the child process and passing the message to the websocket
    child_read_task = asyncio.create_task(read_from_child(unique_id, websocket, out_queue))
    # wait for both coroutines to complete
    await asyncio.gather(websocket_task, child_read_task)

    # TODO:
    # The issue is that the child process is still running after the connection is closed
    # we need to kill it uppon abbort...
    # but than the connection is still open...

    



    

print("Starting server...")
start_server = websockets.serve(handle_connection, "0.0.0.0", 8765, max_size=2_000_000_000)
asyncio.get_event_loop().run_until_complete(start_server)
asyncio.get_event_loop().run_forever()


# #let's get the particles 
# particles = manager.config_info().particles()

# print(
#     dir(particles[0])
# )
# for p in particles:
#     ppos = np.array(p.pos)
   
#     a1 = [
#         p.orientation[0][0],
#         p.orientation[1][0],
#         p.orientation[2][0]
#     ]
#     a3 = [
#         p.orientation[0][2],
#         p.orientation[1][2],
#         p.orientation[2][2]
#     ]
#     a(f"{ppos[0]} {ppos[1]} {ppos[2]} {a1[0]} {a1[1]} {a1[2]} {a3[0]} {a3[1]} {a3[2]} 0 0 0 0 0 0")
  