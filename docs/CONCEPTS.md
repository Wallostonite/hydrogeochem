# The Idea, in Plain English

A companion to `ARCHITECTURE.md`, written for anyone who wants to understand what this
system is doing and why, without the engineering vocabulary.

---

## The goal, and who it is for

Almost everyone depends on water, and almost everyone shares one question about it: will it
stay safe on the way to me? A big part of that question is chemistry. Water that is out of
balance eats into pipes and pulls metal, including lead, into what comes out of the tap; water
that is over-saturated clogs pipes and equipment with scale. The chemistry that decides which
way it goes is invisible in a lab report but entirely predictable.

The goal of this project is to make that hidden chemistry understandable and predictable, so
the people who depend on water can act before a problem reaches the tap. The same answer serves
a household worrying about lead, a utility operator tuning corrosion control, a farmer judging
whether water will harm a field, and a regulator comparing many sites over time. Today the tool
answers the corrosion-and-scaling question reliably; the direction of travel is to broaden
toward overall water-quality prediction, so it speaks to more of those needs over time.

## What the thing is for

A hydrogeologist wants to answer a question like: *is the groundwater at this well going
to clog my pipes with limestone, or eat through them?*

Answering it takes two things. First, measurements, how much calcium, sulfate, chloride
and so on is dissolved in the water. The US Geological Survey has been collecting these for
decades and now publishes them through its modern Water Data for the Nation service. Second, a
calculation: given those measurements, which minerals will dissolve and which will crystallise
out. There is a famous piece of government software called PHREEQC that does exactly this
calculation, and has done since the 1980s.

> A practical note: the USGS retired its old data websites and moved everything to a new
> platform (Water Data for the Nation). This tool reads from the new one, so a "USGS" site
> returns real measurements. Because a single visit rarely measures everything, the tool has
> a *finder* that locates sites which actually have the measurements you need.

The gap between the two is entirely manual. Someone downloads a spreadsheet, retypes
numbers into a text file in PHREEQC's own input language, runs it, and reads the output.
It takes twenty minutes per site, and it goes wrong in ways nobody notices.

This system closes that gap: fetch the measurements, write the input file correctly, run
the calculation, show the answer.

---

## The one job that actually matters

It would be easy to think the hard part is running PHREEQC. It isn't. PHREEQC works fine.

The hard part is **asking it a well-formed question**, because laboratory data does not
arrive in the form the calculation needs.

Three examples of what goes wrong:

- **Different scales.** Dissolved iron is usually reported in micrograms per litre;
  calcium in milligrams per litre. They look identical in a spreadsheet, just a number in
  a column. Feed the iron number in without converting and you have overstated it by a
  factor of a thousand. PHREEQC will accept it, complete happily, and give you a confident
  answer that is nonsense.

- **Different ways of describing the same thing.** Nitrate can be reported as the whole
  nitrate molecule, or as just the nitrogen in it, a factor of 4.4 apart. Alkalinity is
  conventionally reported "as calcium carbonate", which is a convenient fiction rather
  than what is actually in the water; converted properly it's 22% larger. Sulfate has the
  same issue. Nothing in the data announces which convention was used, and PHREEQC
  assumes whatever its default happens to be unless you tell it explicitly.

- **Missing pieces.** If nobody measured alkalinity, any conclusion about limestone is
  guesswork, but the software will still produce a number, and a number on a screen looks
  like knowledge.

So the centre of this system is a translator that knows, for every measurement type, what
scale it's on, what convention it's reported in, and what it's called in PHREEQC's own
vocabulary. Everything else is scaffolding around that translator.

---

## Why it's split into four pieces

The original prototype did everything in one program: fetched the data, ran the
calculation, drew the charts. That's the right way to start and the wrong way to finish,
for the same reason a food truck is a bad hospital.

The production version separates four jobs that have genuinely different needs:

**The screen** shows things to people. It needs to be quick to change and pretty. It knows
no chemistry at all, it asks the next piece for everything and displays what comes back.

**The front desk** takes requests, checks who's asking, and decides what to do with them.
It's the only piece exposed to the outside world.

**The calculators** do the actual PHREEQC runs. They're slow, they hog a processor, and
they occasionally get stuck, so they're kept well away from anything that needs to stay
responsive.

**The knowledge**, the units, the conventions, the sanity checks, sits underneath all
three, and deliberately has no ability to reach the internet, the database, or anything
else. That sounds like a limitation; it's the point. Because it can't touch the outside
world, its correctness can be checked completely and instantly, without a network, a
database, or PHREEQC installed. The chemistry is the part that must never be wrong, so it
is the part built to be testable in full.

A useful way to picture the arrangement: knowledge at the bottom, machinery in the middle,
people at the top, and information only ever flows *downward* in terms of dependency. The
chemistry never has to know that a web page exists.

---

## The calculator problem

PHREEQC is a program from an era with different assumptions. Three of its habits shape the
whole design:

1. **It can only think about one thing at a time.** It keeps its working state internally,
   so if two users' calculations run through the same copy simultaneously, they contaminate
   each other. The answers come back plausible and wrong.

2. **You can't interrupt it.** Once it starts a calculation, there is no polite way to ask
   it to stop. If a badly-conditioned model sends it into a loop, it will loop forever.

3. **It's slow to wake up.** Loading its reference tables of thermodynamic data, the
   physical constants that make the calculation possible, takes a noticeable moment.

The answer to all three is the same: give each calculation its own isolated booth, with
its own private copy of PHREEQC and its own copy of the reference tables kept warm between
jobs. If a booth locks up, we don't try to reason with it, we demolish the booth and
build a new one. Wasteful, but it's the only thing that actually works, and it means one
bad model can never freeze the service for everyone else.

The number of booths is set to the number of processors available, and no more. Each
calculation uses a whole processor flat out, so running more booths than processors makes
every calculation slower and eventually pushes them past their time limit. This is the
single most common way to accidentally break the system, which is why it's written down.

---

## Letting experts write their own instructions

Experienced users want to write PHREEQC input by hand, mixing two waters, simulating a
titration, things the point-and-click form doesn't cover. That has to be supported.

But PHREEQC's input language can also read and write files anywhere on the machine. Handing
a stranger a text box that runs on our server is, without precautions, handing them the
server.

So there are two independent locks. The first reads the submitted text and refuses anything
containing the file-touching commands. The second gives the booth process a hard, operating
system-enforced allowance of zero bytes of file writing, so even a command we failed to
anticipate simply cannot do damage. Neither lock is trusted on its own. That's the whole
idea behind the phrase *defence in depth*: assume each guard will eventually miss something,
and arrange for that to be survivable.

---

## Never doing the same sum twice

Every calculation gets a fingerprint, made by mashing together four things: the exact input
text, which set of reference tables was used, the version of those tables, and the version
of PHREEQC itself.

If a fingerprint has been seen before, the stored answer is handed back instantly. This
makes duplicate clicks free, makes retries after a network hiccup harmless, and means a
model somebody else already ran costs nothing to look up.

Including the reference tables in the fingerprint is the subtle part. Those tables get
revised. If they change and we kept using the old answer, we'd be quietly serving stale
science with no indication anything had shifted. Tying the identity of an answer to the
exact data that produced it means an update *automatically* invalidates everything derived
from the old version, without anyone having to remember.

---

## The counter and the ticket

Most of these calculations finish in a fraction of a second. Some, reaction paths, kinetics,
anything simulating change over time, take minutes.

Rather than force everyone to wait for the slowest case, the front desk looks at what's
being asked and sorts it. Quick questions are answered while you stand there. Slow ones get
a ticket number and go into a queue handled by separate machines, and you check back. Sorting
is deliberately pessimistic: anything that even *might* be slow gets a ticket, because a
wrong guess in that direction costs a few seconds of waiting, while a wrong guess in the
other direction ties up the front desk.

---

## Three habits that keep the science honest

**Show the working.** The exact PHREEQC input is always available before and after the run.
A result you can't inspect is a result you can't defend in a report, and a black box that
produces saturation indices is worse than no tool at all.

**Check the books balance.** Water is electrically neutral: the dissolved positives and
negatives must cancel out. If the analysis is incomplete or something was mismeasured, the
tally doesn't come out even, exactly like a receipt that doesn't add up. The system
computes this for every sample, states it plainly, and flags anything beyond ten percent
rather than burying it. It's the closest thing water chemistry has to a built-in lie detector.

**Say when you don't know.** An empty result must never be ambiguous between "there is no
data at this site" and "we couldn't reach the data service". The prototype conflated the two,
which is the most dangerous kind of bug in a scientific tool: it doesn't look like a failure.
Every failure now has a specific name, the calculation timed out, the input was rejected, the
upstream service is down, and that name reaches the user.

---

## When things go wrong

The design assumes failure is routine rather than exceptional, and gives each kind of failure
a planned response. The government data service goes down: retry a few times, then serve
slightly stale cached data and say so. A calculation hangs: kill it, rebuild the booth, tell
the user it timed out and suggest simplifying. One site in a batch of two hundred has corrupt
data: record that one as failed and carry on with the other 199.

That last one is a small decision with a large effect. A batch that gives up entirely because
of one bad site wastes an afternoon; a batch that reports 199 results and one clearly-labelled
failure is just a normal day.

---

## How it grows

The current version handles the common case: what's dissolved in this water, and what minerals
is it inclined to form or dissolve. The natural extensions, mixing two waters, simulating a
titration, modelling how chemistry changes as water flows through rock, and working backwards
from observed changes to infer what reactions caused them, all need the same three
foundations that are already built: correct translation of measurements, isolated calculation,
and answers tied permanently to the data that produced them.

The scaffolding was the expensive part. The rest is filling it in.

---

## The jargon, translated

| What engineers call it | What it means here |
|---|---|
| Domain layer | The chemistry knowledge, sealed off so it can be tested completely |
| Ports and adapters | Standard sockets, so the data source can be swapped without touching anything else |
| Process isolation | Each calculation gets its own booth so they can't contaminate each other |
| Idempotency | Asking the same question twice costs nothing and gives the same answer |
| Defence in depth | Two independent locks, because either one will eventually fail |
| Sandboxing | The calculation runs with its hands tied, it physically cannot write files |
| Observability | The system reports on its own health, so problems are noticed before users complain |
| Graceful degradation | When something breaks, the parts that still work keep working |
| Horizontal scaling | Handle more load by adding more machines, not bigger ones |
| Reproducibility | The same question, asked next year, gives the same answer, or explains why not |
