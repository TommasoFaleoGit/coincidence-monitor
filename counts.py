from TimeTagger import createTimeTagger, createTimeTaggerNetwork, ChannelEdge, Countrate

# Initialize the time tagger
tagger = createTimeTagger()

# Get channels and decide the integration time
input_channels = tagger.getChannelList(ChannelEdge.Rising)
integration_time = 5e12  # 5 seconds in picoseconds

# Start counting
counting = Countrate(tagger, input_channels)
counting.startFor(integration_time)
counting.waitUntilFinished()

# Retrieve data
counts = counting.getData()