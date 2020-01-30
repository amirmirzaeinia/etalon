#!/usr/bin/env python

import collections
import glob
from os import path
import socket
from struct import unpack
import sys
# Directory containing this program.
PROGDIR = path.dirname(path.realpath(__file__))
# For python_config.
sys.path.insert(0, path.join(PROGDIR, "..", "etc"))

import numpy as np

import python_config

PERCENTILES = [25, 50, 75, 99, 99.9, 99.99, 99.999, 100]
RTT = python_config.CIRCUIT_LATENCY_s_TDF * 2
# 1/1000 seconds.
BIN_SIZE_MS = 1
# The rack pair to examine when parsing sequence logs. Rack 1 to rack 2.
SR_RACKS = (1, 2)


def parse_flowgrind_config(fln):
    flows = collections.defaultdict(list)
    sizes = {}
    for l in open(fln.split(".txt")[0] + ".config.txt"):
        l = l.split("-F")[1:]
        for x in l:
            hid = int(x.strip().split()[0])
            byts = int(x.strip().split("-Gs=q:C:")[1].split()[0])
            sizes[hid] = byts
    for l in open(fln):
        if "seed" in l:
            hid = int(l[4:].strip().split()[0])
            rack = int(l.split("/")[0].split()[-1].split(".")[2])
            flows[hid].append(rack)
            if l.split(":")[0][-1] == "S":
                time = float(l.split("duration = ")[1].split("/")[0]) * 1000000
                tp = float(l.split("through = ")[1].split("/")[0]) / 1000.
            else:
                flows[hid].append(time)
                flows[hid].append(tp)
                flows[hid].append(sizes[hid])

    for f in flows.keys():
        if flows[f][3] == float("inf"):
            del flows[f]
    tps = [f[3] for f in flows.values()]
    durs = [f[2] for f in flows.values()]
    byts = [f[4] for f in flows.values()]

    print(len(tps), len(durs), len(byts))

    return tps, durs, byts


def msg_from_file(filename, chunksize):
    with open(filename, "rb") as f:
        while True:
            chunk = f.read(chunksize)
            if chunk:
                yield chunk
            else:
                break


def get_seq_data(fln, dur, log_pos="after", msg_len=112, clean=True):
    log_poss = ["before", "after"]
    assert log_pos in log_poss, \
        "Log position must be one of {}, but is: {}".format(log_poss, log_pos)

    print("Parsing: {}".format(fln))
    circuit_starts = collections.defaultdict(list)
    circuit_ends = collections.defaultdict(list)
    flows = collections.defaultdict(list)
    seen = collections.defaultdict(list)
    for msg in msg_from_file(fln, msg_len):
        if msg_len == 112:
            # type (int), timestamp (char[32]), latency (int), src (int),
            # dst (int), data (char[64])
            t, ts, _, src, dst, data = unpack("i32siii64s", msg)
            voq_len = 0
        elif msg_len == 116:
            # type (int), timestamp (char[32]), latency (int), src (int),
            # dst (int), VOQ length (int), data (char[64])
            t, ts, _, src, dst, voq_len, data = unpack("i32siiii64s", msg)
        else:
            raise Exception(
                "Message length must be either 112 or 116, but is: {}".format(
                    msg_len))

        ip_bytes = unpack("!H", data[2:4])[0]

        sender = socket.inet_ntoa(data[12:16])
        recv = socket.inet_ntoa(data[16:20])
        proto = ord(data[9])
        ihl = (ord(data[0]) & 0xF) * 4
        sport = 0
        dport = 0
        seq = 0
        thl = 0
        if proto == 6:  # TCP
            sport = unpack("!H", data[ihl:ihl+2])[0]
            dport = unpack("!H", data[ihl+2:ihl+4])[0]
            seq = unpack("!I", data[ihl+4:ihl+8])[0]
            thl = (ord(data[ihl+12]) >> 4) * 4
        byts = ip_bytes - ihl - thl

        for i in xrange(len(ts)):
            if ord(ts[i]) == 0:
                break
        ts = float(ts[:i]) / python_config.TDF
        # ts = float(ts[:20].strip('\x00')) / python_config.TDF

        if t == 1 or t == 2:
            # Start or end of a circuit.
            sr_racks = (src, dst)
            if t == 1:
                # Circuit start.
                circuit_starts[sr_racks].append(ts)
            elif t == 2:
                # Circuit end.
                if not circuit_starts[sr_racks]:
                    # If we do not have any circuit starts yet, then skip this
                    # circuit end.
                    continue
                circuit_ends[sr_racks].append(ts)
            continue

        flow = (sender, recv, proto, sport, dport)
        if clean:
            if flows[flow]:
                # Extract previous datapoint.
                _, last_seq, last_bytes, _ = flows[flow][-1]

                # Check whether the current sequence number equals the last sequence
                # number plus the number of data bytes in the last packet (i.e.,
                # check that this actually is the next packet, in case the log
                # messages are out of order).
                if abs(last_seq + last_bytes - seq) < 2:
                    # Yes, it is the next packet.
                    updated = True
                    while updated:
                        updated = False
                        # Look over unmatched packets until we find one that...?
                        for prev_seen in seen[flow]:
                            _, prev_seq, prev_bytes, prev_voq_len = prev_seen
                            # Check if this earlier packet actually came immediately
                            # before the current packet.
                            if abs(seq + byts - prev_seq) < 2:
                                # Change the current seq, byts, and voq_len. Does
                                # this have something to do with packet reordering?
                                seq = prev_seq
                                byts = prev_bytes
                                voq_len = prev_voq_len
                                seen[flow].remove(prev_seen)
                                updated = True
                                break
                    flows[flow].append((ts, seq, byts, voq_len))
                else:
                    # No, it is not the next packet. Save it for later.
                    seen[flow].append((ts, seq, byts, voq_len))
            else:
                # First timestamp for this flow.
                flows[flow].append((ts, seq, byts, voq_len))
        else:
            # Do not perform flow cleaning. Record all packets.
            flows[flow].append((ts, seq, byts, voq_len))

    # Validate the circuit starts and ends.
    for sr_racks in circuit_starts.keys():
        num_starts = len(circuit_starts[sr_racks])
        num_ends = len(circuit_ends[sr_racks])

        if num_starts > num_ends:
            print("For {}, discarding {} circuit start!".format(
                sr_racks, num_starts - num_ends))
            circuit_starts[sr_racks] = circuit_starts[sr_racks][:num_ends]
        elif num_ends > num_starts:
            print("For {}, discarding {} circuit end!".format(
                sr_racks, num_ends - num_starts))
            circuit_ends[sr_racks] = circuit_ends[sr_racks][:num_starts]

        # Recompute after modifying.
        num_starts = len(circuit_starts[sr_racks])
        num_ends = len(circuit_ends[sr_racks])
        # Verify that there are the same number of starts and ends.
        assert num_starts == num_ends, \
            "Differing numbers of circuit starts ({}) and ends ({})!".format(
                num_starts, num_ends)
        # Check that all circuit durations are positive.
        diffs = (np.asarray(circuit_ends[sr_racks]) -
                 np.asarray(circuit_starts[sr_racks]))
        assert (diffs > 0).all(), \
            ("Not all circuits have positive duration (i.e., there are "
             "mismatched starts and ends)!")
    print("Circuit starts/ends: {}".format(len(circuit_starts[SR_RACKS])))

    # Calculate stats about the days and weeks.
    starts = []
    ends = []
    nxt_starts = []
    nxt_ends = []
    nxt_nxt_starts = []
    nxt_nxt_ends = []
    day_lens = []
    week_lens = []
    for i in xrange(1, len(circuit_starts[SR_RACKS]) - 2):
        # Use the end of the previous circuit as the relative starting point.
        prev_end = circuit_ends[SR_RACKS][i-1]

        cur_start = circuit_starts[SR_RACKS][i]
        cur_end = circuit_ends[SR_RACKS][i]

        nxt_start = circuit_starts[SR_RACKS][i + 1]
        nxt_end = circuit_ends[SR_RACKS][i + 1]

        nxt_nxt_start = circuit_starts[SR_RACKS][i + 2]
        nxt_nxt_end = circuit_ends[SR_RACKS][i + 2]

        starts.append((cur_start - prev_end) * 1e6)
        ends.append((cur_end - prev_end) * 1e6)

        nxt_starts.append((nxt_start - prev_end) * 1e6)
        nxt_ends.append((nxt_end - prev_end) * 1e6)

        nxt_nxt_starts.append((nxt_nxt_start - prev_end) * 1e6)
        nxt_nxt_ends.append((nxt_nxt_end - prev_end) * 1e6)

        day_lens.append((cur_end - cur_start) * 1e6)
        week_lens.append((nxt_start - cur_start) * 1e6)
    # Compute average start/end times and day/week lengths. Remove the first and
    # last five datapoints.
    starts_avg = np.average(starts[5:-5])
    ends_avg = np.average(ends[5:-5])
    nxt_starts_avg = np.average(nxt_starts[5:-5])
    nxt_ends_avg = np.average(nxt_ends[5:-5])
    nxt_nxt_starts_avg = np.average(nxt_nxt_starts[5:-5])
    nxt_nxt_ends_avg = np.average(nxt_nxt_ends[5:-5])
    day_lens = day_lens[5:-5]
    week_lens = week_lens[5:-5]
    print("day avg: {}, std dev: {}".format(
        np.average(day_lens), np.std(day_lens)))
    print("week avg: {}, std dev: {}".format(
        np.average(week_lens), np.std(week_lens)))
    print(("starts avg: {}, ends avg: {}, nxt_starts avg: {}, "
           "nxt_ends avg: {}, nxt_nxt_starts avg: {}, "
           "nxt_nxt_ends avg: {}").format(
               starts_avg, ends_avg, nxt_starts_avg, nxt_ends_avg,
               nxt_nxt_starts_avg, nxt_nxt_ends_avg))

    print("Found {} flows".format(len(flows)))
    timing_offset = python_config.CIRCUIT_LATENCY_s \
        if log_pos == "after" else 0
    results = {}
    for f in flows.keys():
        if "10.1.2." in f[0]:
            # Skip the reverse flows (i.e., skip the ACKs, since we only care
            # about the data packets).
            print("Skipping flow: {}".format(f))
            continue
        else:
            print("Parsing flow: {}".format(f))

        # Interpolated and uninterpolated (i.e., original) chunks for this flow.
        chunks_interp = []
        chunks_orig = []
        # The idx of the last datapoint in the previous chunk.
        last_idx = 0
        # The number of chunks with no data (i.e., bad chunks).
        bad_chunks = 0
        first_ts = flows[f][0][0]
        last_ts = flows[f][-1][0]
        for i in xrange(1, len(circuit_starts[SR_RACKS]) - 2, 3):
            prev_end = circuit_ends[SR_RACKS][i - 1]
            cur_start = circuit_starts[SR_RACKS][i]
            cur_end = circuit_ends[SR_RACKS][i]
            nxt_nxt_end = circuit_ends[SR_RACKS][i+2]

            # We skip the current chunk if the end of the current circuit is
            # earlier than the first timestamp, or the start of the current
            # circuit is later than the last timestamp, or there are fewer than
            # two circuits after the current circuit.
            if not ((cur_end < first_ts) or
                    (cur_start > last_ts) or
                    (i + 1 >= len(circuit_ends[SR_RACKS])) or
                    (i + 2 >= len(circuit_ends[SR_RACKS]))):
                # A list of pairs where the first element is a relative
                # timestamp and the second element is a relative sequence
                # number.
                out = []
                # The first (absolute) sequence number in this flow.
                first_seq = -1
                for idx in xrange(last_idx, len(flows[f])):
                    ts, seq, _, voq_len = flows[f][idx]
                    if ts > nxt_nxt_end + timing_offset:
                        # The timestamp is too late, so we drop this
                        # datapoint. We are done with the current chunk.
                        break
                    elif ts >= prev_end + timing_offset:
                        # The timestamp is in the valid range.
                        rel_ts = (ts - prev_end - timing_offset) * 1e6
                        if first_seq == -1:
                            first_seq = seq
                            # print("First seq num in circuit {}: {}".format(
                            #     i, first_seq))
                            # print("rel_ts: {}".format(rel_ts))
                        rel_seq = seq - first_seq
                        if out and rel_ts == out[-1][0]:
                            # Do not add this result if there already exists a
                            # value for this timestamp.
                            print(("Warning: Dropping datapoint ({}, {}) "
                                   "because we already have data for that "
                                   "timestamp!").format(
                                       rel_ts, rel_seq))
                            continue
                        out.append((rel_ts, rel_seq, voq_len))
                    else:
                        # The timestamp is too early, so we drop this
                        # datapoint. We are before the current chunk.
                        pass

                    if ts < cur_end + timing_offset:
                        # The timestamp is the latest timestamp we have seen, so
                        # record its idx so that we avoid doing duplicate work.
                        last_idx = idx
                if not out:
                    # No data for this chunk.
                    bad_chunks += 1
                    out = [(0, 0, 0), (dur, 0, 0)]
                wraparound = False
                for _, seq, _ in out:
                    if seq < -1e8 or seq > 1e8:
                        wraparound = True
                if wraparound:
                    print("Warning: Wraparound detected. Dropping results!")
                else:
                    # This is valid data, so we will store it. Sort the data so
                    # that it can be used by numpy.interp().
                    out = sorted(out, key=lambda a: a[0])
                    xs, ys, voq_lens = zip(*out)
                    diffs = np.diff(xs) > 0
                    if not np.all(diffs):
                        print("diffs: {}".format(diffs))
                        print("out:\n{}".format(
                            "\n".join([
                                "    x: {}, y: {}, voq len: {}".format(x, y, v)
                                for x, y, v in out])))
                        raise Exception(
                            "numpy.interp() requires x values to be increasing")
                    # Interpolate based on the data that we have.
                    new_xs = xrange(dur)
                    chunks_interp.append(
                        (np.interp(new_xs, xs, ys),
                         np.interp(new_xs, xs, voq_lens)))
                    # Also record the original (uninterpolated) chunk data.
                    chunks_orig.append((xs, ys, voq_lens))

        print("Chunks for this flow: {}".format(len(chunks_interp)))
        print("Bad chunks for this flow: {}".format(bad_chunks))
        if chunks_interp:
            # If there is data for this flow...
            print("Timestamps for this flow: {}".format(len(chunks_interp[0])))
            # List of lists, where each sublist corresponds to one timestep and each
            # entry of each sublist corresponds to a sequence number for that
            # timestep.
            zipped_seqs, zipped_voqs = zip(*chunks_interp)
            unzipped_seqs = zip(*zipped_seqs)
            unzipped_voqs = zip(*zipped_voqs)
            # Average the sequence numbers for across each timestep, creating the
            # final results for this flow.
            results[f] = ([np.average(seqs) for seqs in unzipped_seqs],
                          [np.average(voqs) for voqs in unzipped_voqs],
                          chunks_orig)

    # # Remove flows that have no datapoints.
    # old_len = len(results)
    # results = {flw: flw_res for flw, flw_res in results.items() if flw_res[0]}
    # len_delta = old_len - len(results)
    # if len_delta:
    #     print("Warning: {} flows were filtered out!".format(len_delta))

    # Extract the chunk results.
    chunks_orig_all = {
        flw: chunks_orig for flw, (_, _, chunks_orig) in results.items()}

    print("Flows for which we have results:\n{}".format(
        "\n".join([
            "    {}: {} chunks".format(
                flw, len(chunks_orig))
            for flw, chunks_orig in chunks_orig_all.items()])))

    # Turn a list of lists of results for each flow into a list of lists of
    # results for all flows at one timestep.
    seqs = zip(*[seqs for seqs, _, _ in results.values()])
    voqs = zip(*[voqs for _, voqs, _ in results.values()])

    # Average the results of all flows at each timestep. So, the output is
    # really: For each timestep, what's the average sequence number of all
    # flows.
    seqs = [np.average(r) for r in seqs]
    voqs = [np.average(r) for r in voqs]
    return (seqs, voqs), (starts_avg, ends_avg, nxt_starts_avg, nxt_ends_avg,
                     nxt_nxt_starts_avg, nxt_nxt_ends_avg), chunks_orig_all


def parse_packet_log(fln):
    print("Parsing: {}".format(fln))
    latencies = []
    latencies_circuit = []
    latencies_packet = []
    throughputs = collections.defaultdict(int)
    circuit_bytes = collections.defaultdict(int)
    packet_bytes = collections.defaultdict(int)
    flow_start = {}
    flow_end = {}
    number_circuit_ups = collections.defaultdict(int)
    circuit_starts = collections.defaultdict(list)
    most_recent_circuit_up = collections.defaultdict(int)
    bytes_in_rtt = collections.defaultdict(lambda: collections.defaultdict(int))
    for msg in msg_from_file(fln, chunksize=112):
        (t, ts, lat, src, dst, data) = unpack("i32siii64s", msg)
        byts = unpack("!H", data[2:4])[0]
        circuit = ord(data[1]) & 0x1
        sender = ord(data[14])
        recv = ord(data[18])
        ts = float(ts[:20].strip("\x00"))
        if t == 1 or t == 2:  # starting or closing
            sr_racks = (src, dst)
            if t == 1:  # starting
                most_recent_circuit_up[sr_racks] = ts
            if t == 2:  # closing
                circuit_starts[sr_racks].append(ts / python_config.TDF)
                number_circuit_ups[sr_racks] += 1
            continue

        latency = float(lat)
        sr = (sender, recv)
        if byts < 100:
            continue
        if sr not in flow_start:
            flow_start[sr] = ts / python_config.TDF

        if circuit:
            which_rtt = int((ts - most_recent_circuit_up[sr] - 0.5 * RTT) / RTT)
            bytes_in_rtt[sr][which_rtt] += byts
            circuit_bytes[sr] += byts
        else:
            packet_bytes[sr] += byts

        throughputs[sr] += byts
        flow_end[sr] = ts / python_config.TDF
        if byts > 1000:
            latencies.append(latency)
            if circuit:
                latencies_circuit.append(latency)
            else:
                latencies_packet.append(latency)
    lat = [(perc, np.percentile(latencies, perc)) for perc in PERCENTILES]
    latc = [(p, 0) for p in PERCENTILES]
    if latencies_circuit:
        latc = [(perc, np.percentile(latencies_circuit, perc))
                for perc in PERCENTILES]
    latp = [(perc, np.percentile(latencies_packet, perc))
            for perc in PERCENTILES]

    tp = {}
    b = collections.defaultdict(dict)
    p = {}
    c = {}
    for sr in flow_start:
        total_time = flow_end[sr] - flow_start[sr]
        tp[sr] = throughputs[sr] / total_time
        tp[sr] *= 8  # bytes to bits
        tp[sr] /= 1e9  # bits to gbits

        p[sr] = (packet_bytes[sr] / total_time) * 8 / 1e9
        c[sr] = (circuit_bytes[sr] / total_time) * 8 / 1e9

        n = 0
        for ts in circuit_starts[sr]:
            if ts >= flow_start[sr] and ts <= flow_end[sr]:
                n += 1
        max_bytes = n * RTT * (python_config.CIRCUIT_BW_Gbps_TDF * 1e9 / 8.)
        for i, r in sorted(bytes_in_rtt[sr].items()):
            b[sr][i] = (r / max_bytes) * 100

    return tp, (lat, latc, latp), p, c, b, \
        sum(circuit_bytes.values()), sum(packet_bytes.values())


def parse_validation_log(fln, dur_ms=1300, bin_size_ms=1):
    print("Parsing: {}".format(fln))
    # Map of flow ID to pair (src rack, dst rack).
    id_to_sr = {}
    with open(fln.strip(".txt") + ".config.txt") as f:
        for line in f:
            line = line.split("-F")[1:]
            for flow_cnf in line:
                flow_cnf = flow_cnf.strip()
                idx = int(flow_cnf.split()[0])
                # Extract src rack id.
                s = int(flow_cnf.split("-Hs=10.1.")[1].split(".")[0])
                # Extract dst rack id.
                r = int(flow_cnf.split(",d=10.1.")[1].split(".")[0])
                id_to_sr[idx] = (s, r)

    # Map of pair (src rack, dst rack) to a map of window *start* timestamp to
    # the number of bytes sent during the window beginning with this timestamp.
    sr_to_tstamps = collections.defaultdict(
        lambda: collections.defaultdict(int))
    # Map of emulated src idx (i.e., flow idx) to a map of window *end*
    # timestamp to the src's CWND at the timestamp (i.e., at the time that the
    # window ended).
    flow_to_cwnds = collections.defaultdict(
        lambda: collections.defaultdict(int))
    with open(fln) as f:
        for line in f:
            # Ignore comment lines, empty lines, and "D" lines.
            if line[0] == "S":
                line = line[1:].strip()
                splits = line.split()
                # Flow ID.
                idx = int(splits[0])
                # Start timestamp for this window.
                ts_s_start = float(splits[1])
                # End timestamp for this window.
                ts_s_end = float(splits[2])
                # Throughput for this window.
                cur_tput_mbps = float(splits[3])
                # Bits sent during this window. Mb/s -> b. Do not divide by TDF.
                cur_b = cur_tput_mbps * 1e6  * (ts_s_end - ts_s_start)
                # Record the bytes sent during this window.
                sr_to_tstamps[id_to_sr[idx]][ts_s_start] += cur_b
                # CWND at the end of this window.
                cwnd = int(splits[11])
                # Record this flow's cwnd at the end of this window.
                flow_to_cwnds[idx][ts_s_end] = cwnd
    # For each (src, dst) rack pair, transform the mapping from timestamp to
    # bytes into a list of sorted pairs of (timestamp, bytes).
    sr_to_tstamps = {sr: sorted(tstamps.items())
                     for sr, tstamps in sr_to_tstamps.items()}
    # For each flow, transform the mapping from timestamp to cwnd into a list of
    # sorted pairs of (timestamp, cwnd).
    flow_to_cwnds = {flw: sorted(cwnds.items())
                     for flw, cwnds in flow_to_cwnds.items()}

    # Map of pair (src rack, dst rack) to list of throughputs in Gb/s. Each
    # throughput corresponds to the throughput during one bin.
    sr_to_tputs = collections.defaultdict(list)
    for sr, tstamps in sr_to_tstamps.items():
        for win_start_ms in xrange(0, dur_ms, bin_size_ms):
            win_start_s = win_start_ms / 1e3
            win_end_s = win_start_s + (bin_size_ms / 1e3)
            # Find all the logs (i.e., numbers of bytes during some window) for
            # the current bin...
            cur_samples = [b for ts_s, b in tstamps
                           # E.g., for i = 1 and BIN_SIZE_MS = 1, if timestamp
                           # is between 1 ms and 2 ms.
                           if win_start_s <= ts_s < win_end_s]
            # ...and sum them up to calculate the total number of bytes sent in
            # this bin (e.g., if the bin size is 1, then this is the total
            # amount of bytes sent in 1 ms).
            cur_b = sum(cur_samples)
            gbps = cur_b * (1 / BIN_SIZE_MS) * 1e3 / 1e9
            sr_to_tputs[sr].append((win_start_ms / 1e3, gbps))

    means = {}
    stdevs = {}
    for sr, tputs in sr_to_tputs.items():
        # Jump over data from before the flows started.
        start_idx = 0
        for _, tput in tputs:
            if round(tput) > 0:
                break
            start_idx += 1
        # Extract the tputs from of list of pairs of (window start time, tput).
        _, tputs = zip(*tputs[start_idx:])
        means[sr] = np.mean(tputs)
        stdevs[sr] = np.std(tputs)

    print("Num tput logs:")
    for sr, tputs in sr_to_tputs.items():
        print("  (src rack, dst rack): {} = {}".format(sr, len(tputs)))
    print("Means:")
    for sr, mean in means.items():
        print("  (src rack, dst rack): {} = {} Gb/s".format(sr, mean))
    print("Standard deviations:")
    for sr, stdev in stdevs.items():
        print("  (src rack, dst rack): {} = {} Gb/s".format(sr, stdev))
    print("Mean of means: {} Gb/s".format(np.mean(means.values())))
    print(
        "Mean of standard deviations: {} Gb/s".format(np.mean(stdevs.values())))

    return sr_to_tputs, flow_to_cwnds


def parse_hdfs_logs(folder):
    data = []
    durations = []

    fln = folder + "/*-logs/hadoop*-datanode*.log"
    print(fln)
    logs = glob.glob(fln)

    for log in logs:
        for line in open(log):
            if "clienttrace" in line and "HDFS_WRITE" in line:
                byts = int(line.split("bytes: ")[1].split()[0][:-1])
                if byts > 1024 * 1024 * 99:
                    duration = ((float(line.split(
                        "duration(ns): ")[1].split()[0]) * 1e-6) /
                                python_config.TDF)
                    data.append((byts / 1024. / 1024., duration))

    durations = sorted(zip(*data)[1])
    return durations


def parse_hdfs_throughput(folder):
    fln = folder + "/report/dfsioe/hadoop/bench.log"
    logs = glob.glob(fln)

    for log in logs:
        for line in open(log):
            if "Number of files" in line:
                num_files = int(line.split("files: ")[1])
            if "Throughput mb/sec:" in line:
                # divide by number of replicas
                return (float(line.split("mb/sec:")[1]) *
                        num_files * 8 / 1024.) * python_config.TDF / 2.
