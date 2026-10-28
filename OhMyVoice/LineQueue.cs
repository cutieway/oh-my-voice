namespace OhMyVoice;

/// <summary>
/// FIFO of accepted lines for queue mode. The dispatcher reserves a ticket when
/// a line arrives, publishes the resolved file (or a null pair when the line is
/// dropped), and the pump plays the results one at a time in arrival order,
/// after the previous line ends plus the configured gap.
///
/// Ordering is by ticket, not by publication time, so a line whose voice-file
/// lookup finishes late still keeps its place in the conversation. Every ticket
/// must be published exactly once (a second publish is ignored); the pump would
/// otherwise wait forever for it.
/// </summary>
public sealed class LineQueue
{
    private readonly object gate = new();
    private readonly SortedList<long, Item> pending = new();
    private readonly int capacity;
    private readonly Func<CancellationToken, Task> waitIdle;
    private readonly Func<int, CancellationToken, Task> waitGap;
    private readonly Func<int> gapMs;
    private readonly Func<bool>? enabled;
    private readonly Action<string, string> play;
    private readonly Action<string>? log;
    private long seq;
    private bool running;

    private sealed class Item
    {
        public readonly TaskCompletionSource<bool> Done =
            new(TaskCreationOptions.RunContinuationsAsynchronously);
        public string? Id;
        public string? Path;
        public bool Published;
    }

    public LineQueue(
        int capacity,
        Func<CancellationToken, Task> waitIdle,
        Func<int, CancellationToken, Task> waitGap,
        Func<int> gapMs,
        Action<string, string> play,
        Func<bool>? enabled = null,
        Action<string>? log = null)
    {
        this.capacity = Math.Max(1, capacity);
        this.waitIdle = waitIdle;
        this.waitGap = waitGap;
        this.gapMs = gapMs;
        this.play = play;
        this.enabled = enabled;
        this.log = log;
    }

    /// <summary>Lines waiting for their turn, including ones still resolving.</summary>
    public int Count
    {
        get { lock (gate) return pending.Count; }
    }

    /// <summary>Reserve the next position in dialog order.</summary>
    public long Enqueue()
    {
        var item = new Item();
        bool startPump;
        lock (gate)
        {
            pending[++seq] = item;
            // Drop the oldest first: after a long skip streak the newest lines
            // are the ones the player still cares about.
            while (pending.Count > capacity)
            {
                pending.Values[0].Done.TrySetResult(true);
                pending.RemoveAt(0);
            }
            startPump = !running;
            running = true;
        }
        if (startPump) _ = PumpAsync();
        return seq;
    }

    /// <summary>Hand the resolved file to the pump; nulls mark a dropped line.</summary>
    public void Publish(long ticket, string? id, string? path)
    {
        Item? item;
        lock (gate)
        {
            if (!pending.TryGetValue(ticket, out item) || item.Published) return;
            item.Published = true;
            item.Id = id;
            item.Path = path;
        }
        item.Done.TrySetResult(true);
    }

    /// <summary>Drop every pending line. The line already playing is left alone.</summary>
    public void Clear()
    {
        List<Item> dropped;
        lock (gate)
        {
            if (pending.Count == 0) return;
            dropped = [.. pending.Values];
            pending.Clear();
        }
        foreach (var item in dropped) item.Done.TrySetResult(true);
    }

    private async Task PumpAsync()
    {
        while (true)
        {
            if (enabled is not null && !enabled())
            {
                // Plugin turned off: drop the backlog and stop the pump. A later
                // Enqueue starts a fresh one.
                List<Item> dropped;
                lock (gate)
                {
                    dropped = [.. pending.Values];
                    pending.Clear();
                    running = false;
                }
                foreach (var droppedItem in dropped) droppedItem.Done.TrySetResult(true);
                return;
            }

            long key;
            Item item;
            lock (gate)
            {
                if (pending.Count == 0)
                {
                    running = false;
                    return;
                }
                key = pending.Keys[0];
                item = pending.Values[0];
            }

            await item.Done.Task;

            if (item.Id == null || item.Path == null)
            {
                lock (gate) RemoveIfHead(key);
                continue; // miss, suppressed, or dropped by the cap
            }

            // Wait for the previous line to finish, then the configured gap.
            await waitIdle(CancellationToken.None);
            await waitGap(gapMs(), CancellationToken.None);

            int behind;
            lock (gate)
            {
                // Cleared or dropped while waiting: never play a stale line.
                if (!RemoveIfHead(key)) continue;
                behind = pending.Count;
            }

            log?.Invoke($"{item.Id}: queued, playing ({behind} behind)");
            play(item.Id, item.Path);
        }
    }

    /// <summary>Removes the head only if it is still this ticket. Returns false otherwise.</summary>
    private bool RemoveIfHead(long key)
    {
        if (pending.Count == 0 || pending.Keys[0] != key) return false;
        pending.RemoveAt(0);
        return true;
    }
}
