import sys, os, platform
import math
import traceback
import argparse
import time, random
import logging

from functools import partial
from collections import OrderedDict

import psychopy

psychopy.prefs.general['audioLib'] = ['pyo','pygame', 'sounddevice']

from psychopy import core, visual, gui, data, event, monitors, sound, tools
import numpy

import qcsf, settings, assets

import monitorTools

class Trial():
	def __init__(self, eccentricity, orientation, stimPositionAngle):
		self.eccentricity = eccentricity
		self.orientation = orientation
		self.stimPositionAngle = stimPositionAngle

	def __str__(self):
		return self.__repr__()

	def __repr__(self):
		return f'Trial(e={self.eccentricity},o={self.orientation},a={self.stimPositionAngle})'

class UserExit(Exception):
	def __init__(self):
		super().__init__('User asked to quit.')

def getSound(filename, freq, duration):
	try:
		filename = os.path.join('assets', 'qCSF', filename)
		filename = assets.getFilePath(filename)
		return sound.Sound(filename)
	except ValueError:
		logging.warning(f'Failed to load sound file: {filename}. Synthesizing sound instead.')
		return sound.Sound(freq, secs=duration)

def getConfig():
	config = settings.getSettings('qCSF Settings.ini')
	config['start_time'] = data.getDateStr()
	logFile = config['data_filename'].format(**config) + '.log'
	logging.basicConfig(filename=logFile, level=logging.DEBUG, format='%(asctime)s %(levelname)-8s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

	# Each of these are lists of numbers
	for k in ['eccentricities', 'orientations', 'stimulus_position_angles', 'contrast_overrides']:
		if isinstance(config[k], str):
			config[k] = [float(v) for v in config[k].split()]
		else:
			config[k] = [float(config[k])]

	config['sitmulusTone'] = getSound('600Hz_square_25.wav', 600, .185)
	config['positiveFeedback'] = getSound('1000Hz_sine_50.wav', 1000, .077)
	config['negativeFeedback'] = getSound('300Hz_sine_25.wav', 300, .2)
	
	return config

class PeripheralCSFTester():
	def __init__(self, config):
		self.config = config

		sound.init()

		self.setupMonitor()
		self.setupHUD()
		self.setupDataFile()
		
		self.setupBlocks()

	def setupMonitor(self):
		physicalSize = monitorTools.getPhysicalSize()
		resolution = monitorTools.getResolution()

		self.mon = monitors.Monitor('testMonitor')
		self.mon.setDistance(self.config['monitor_distance'])  # Measure first to ensure this is correct
		self.mon.setWidth(physicalSize[0]/10)
		self.mon.setSizePix(resolution)
		self.mon.save()

		self.win = visual.Window(fullscr=True, monitor='testMonitor', allowGUI=False, units='deg', size=resolution)

		self.stim = visual.GratingStim(self.win, contrast=1, sf=6, size=self.config['stimulus_size'], mask='gauss')
		fixationVertices = (
			(0, -0.5), (0, 0.5),
			(0, 0),
			(-0.5, 0), (0.5, 0),
		)
		self.fixationStim = visual.ShapeStim(self.win, vertices=fixationVertices, lineColor=-1, closeShape=False, size=self.config['fixation_size']/60.0)

		if self.config['wait_for_fixation'] or self.config['render_at_gaze']:
			import PyPupilGazeTracker
			import PyPupilGazeTracker.smoothing
			import PyPupilGazeTracker.PsychoPyVisuals
			import PyPupilGazeTracker.GazeTracker
			
			self.screenMarkers = PyPupilGazeTracker.PsychoPyVisuals.ScreenMarkers(self.win)
			self.gazeTracker = PyPupilGazeTracker.GazeTracker.GazeTracker(
				smoother=PyPupilGazeTracker.smoothing.SimpleDecay(),
				screenSize=resolution
			)
			self.gazeTracker.start()
		else:
			self.gazeTracker = None

	def setTopLeftPos(self, stim, pos):
		# convert pixels to degrees
		stimDim = stim.boundingBox
		screenDim = self.mon.getSizePix()
		centerPos = [
			pos[0] + (stimDim[0] - screenDim[0]) / 2,
			(screenDim[1] - stimDim[1]) / 2 - pos[1],
		]
		stim.pos = centerPos

	def updateHUD(self, item, text, color=None):
		element, pos, labelText = self.hudElements[item]
		element.text = text
		self.setTopLeftPos(element, pos)
		if color != None:
			element.color = color

	def setupHUD(self):
		lineHeight = 40
		xOffset = 225
		yOffset = 10

		self.hudElements = OrderedDict(
			lastStim = [visual.TextStim(self.win, text=' '), [xOffset, 0 + yOffset], 'Last stim'],
			lastResp = [visual.TextStim(self.win, text=' '), [xOffset + 40, lineHeight + yOffset], None],
			lastOk = [visual.TextStim(self.win, text=' '), [xOffset -10, lineHeight + yOffset], 'Last resp'],
			thisStim = [visual.TextStim(self.win, text=' '), [xOffset, 2*lineHeight + yOffset], 'This stim'],
			expectedResp = [visual.TextStim(self.win, text=' '), [xOffset, 3*lineHeight + yOffset], 'Exp resp'],
		)

		for key in list(self.hudElements):
			stim, pos, labelText = self.hudElements[key]
			if labelText is not None:
				label = visual.TextStim(self.win, text=labelText+':')
				pos = [30, pos[1]]
				self.hudElements[key+'_label'] = [label, pos, None]

		for key in list(self.hudElements):
			stim, pos, labelText = self.hudElements[key]

			stim.color = 1
			stim.units = 'pix'
			stim.height = lineHeight * .88
			stim.wrapWidth = 9999
			self.setTopLeftPos(stim, pos)

	def enableHUD(self):
		for key, hudArgs in self.hudElements.items():
			stim, pos, labelText = hudArgs
			stim.autoDraw = True

	def disableHUD(self):
		for key, hudArgs in self.hudElements.items():
			stim, pos, labelText = hudArgs
			stim.autoDraw = False

	def setupDataFile(self):
		self.dataFilename = self.config['data_filename'].format(**self.config) + '.csv'
		logging.info(f'Starting data file {self.dataFilename}')

		if not os.path.exists(self.dataFilename):
			dataFile = open(self.dataFilename, 'w')
			dataFile.write('Eccentricity,Orientation,PeakSensitivity,PeakFrequency,Bandwidth,Delta\n')
			dataFile.close()

	def writeOutput(self, eccentricity, orientation, parameterEstimates):
		logging.debug(f'Saving record to {self.dataFilename}, e={eccentricity}, o={orientation}, p={parameterEstimates.T}')

		dataFile = open(self.dataFilename, 'a')  # a simple text file with 'comma-separated-values'
		dataFile.write(f'{eccentricity},{orientation},{parameterEstimates[0]},{parameterEstimates[1]},{parameterEstimates[2]},{parameterEstimates[3]}\n')
		dataFile.close()

	def setupStepHandler(self):
		# Maximums lowered for older adults
		stimulusSpace = numpy.array([
			numpy.arange(0, 31),	# Contrast
			numpy.arange(0, 20),	# Frequency
		])
		parameterSpace = numpy.array([
			numpy.arange(0, 28),	# Peak sensitivity
			numpy.arange(0, 21),	# Peak frequency
			numpy.arange(0, 21),	# Log bandwidth
			numpy.arange(0, 21)		# Low frequency truncation (log delta)
		])

		logging.info('Stimulus space (contrast): numpy.arange(0, 31))')
		logging.info('Stimulus space (frequency): numpy.arange(0, 20))')
		
		logging.info('Parameter space (peak sensitivity): numpy.arange(0, 28))')
		logging.info('Parameter space (peak frequency): numpy.arange(0, 21))')
		logging.info('Parameter space (log bandwidth): numpy.arange(0, 21))')
		logging.info('Parameter space (log delta): numpy.arange(0, 21))')

		return qcsf.QCSF(stimulusSpace, parameterSpace)

	def showMessage(self, msg):
		instructionsStim = visual.TextStim(self.win, text=msg, color=-1, wrapWidth=40)
		instructionsStim.draw()

		self.win.flip()

		keys = event.waitKeys()
		if 'escape' in keys:
			raise UserExit()

	def showInstructions(self, firstTime=False):
		key1 = self.config['first_stimulus_key_label']
		key2 = self.config['second_stimulus_key_label']

		instructions = 'In this experiment, you will be presented with two options - one will be blank, and the other will be a stimulus.\n\n'
		instructions += 'A tone will play when each option is displayed. After both tones, you will need to select which option contained the stimulus.\n\n'
		instructions += 'If the stimulus appeared during the FIRST tone, press [' + key1.upper() + '].\n'
		instructions += 'If the stimulus appeared during the SECOND tone, press [' + key2.upper() + '].\n\n'
		instructions += 'During the process, keep your gaze fixated on the small cross at the center of the screen.\n\n'
		instructions += 'If you are uncertain, make a guess.\n\n\nPress any key to start.'
		
		if not firstTime:
			instructions = 'These instructions are the same as before.\n\n' + instructions

		self.showMessage(instructions)

	def takeABreak(self, waitForKey=True):
		self.showMessage('Good job - it\'s now time for a break!\n\nWhen you are ready to continue, press the [SPACEBAR].')

	def checkResponse(self, whichStim):
		key1 = self.config['first_stimulus_key']
		key2 = self.config['second_stimulus_key']

		label1 = self.config['first_stimulus_key_label']
		label2 = self.config['second_stimulus_key_label']

		correct = None
		while correct is None:
			keys = event.waitKeys()
			logging.debug(f'Keys detected: {keys}')
			if key1 in keys:
				logging.info(f'User selected key1 ({key1})')
				self.updateHUD('lastResp', label1)
				correct = (whichStim == 0)
			if key2 in keys:
				self.updateHUD('lastResp', label2)
				logging.info(f'User selected key1 ({key2})')
				correct = (whichStim == 1)
			if 'q' in keys or 'escape' in keys:
				raise UserExit()

			event.clearEvents()

		return correct

	def setupBlocks(self):
		'''
			blocks = [
				{'eccentricity': x, 'trials': [trial, trial, trial]},
				{'eccentricity': y, 'trials': [trial, trial, trial]},
				...
			]
		'''
		self.blocks = []
		for eccentricity in self.config['eccentricities']:
			block = {
				'eccentricity': eccentricity,
				'trials': [],
			}
			for orientation in self.config['orientations']:
				possibleAngles = []

				for configTrial in range(self.config['trials_per_stimulus_config']):
					if len(possibleAngles) == 0:
						possibleAngles = list(self.config['stimulus_position_angles'])
						random.shuffle(possibleAngles)

					block['trials'].append(Trial(eccentricity, orientation, possibleAngles.pop()))
				
			random.shuffle(block['trials'])
			self.blocks.append(block)

		random.shuffle(self.blocks)
		
		for block in self.blocks:
			logging.debug('Block eccentricity: {eccentricity}'.format(**block))
			for trial in block['trials']:
				logging.debug(f'\t{trial}')

	def runBlocks(self):
		for blockCounter, block in enumerate(self.blocks):
			# Setup a step handler for each orientation
			stepHandlers = {}
			for orientation in self.config['orientations']:
				stepHandlers[orientation] = self.setupStepHandler()

			# Show instructions
			self.showInstructions(blockCounter==0)
			# Run each trial in this block
			self.enableHUD()
			for trialCounter,trial in enumerate(block['trials']):
				self.fixationStim.draw()
				self.win.flip()
				
				time.sleep(self.config['time_between_stimuli'] / 1000.0)     # pause between trials
				self.runTrial(trial, stepHandlers[trial.orientation])

			self.disableHUD()
			# Write output
			for orientation in self.config['orientations']:
				result = stepHandlers[orientation].getBestParameters().T
				self.writeOutput(block['eccentricity'], orientation, result)

			# Take a break if it's time
			if blockCounter < len(self.blocks)-1:
				logging.debug('Break time')
				self.takeABreak()

		logging.debug('User is done!')

	def runTrial(self, trial, stepHandler):
		stimParams = stepHandler.next()[0]
		# These parameters are indices - not real values. They must be mapped
		stimParams = qcsf.mapStimParams(numpy.array([stimParams]), True)

		if len(config['contrast_overrides']) > 0:
			# This is usually only used in practice mode
			contrast = random.choice(config['contrast_overrides'])
		else:
			if stimParams[0] == 0:
				contrast = 1
			else:
				contrast = 1/stimParams[0] # convert sensitivity to contrast

		frequency = stimParams[1]
		logging.info(f'Presenting ecc={trial.eccentricity}, orientation={trial.orientation}, contrast={contrast}, frequency={frequency}, positionAngle={trial.stimPositionAngle}')
		whichStim = int(random.random() * 2)

		self.stim.sf = frequency
		self.stim.ori = trial.orientation
		self.stim.pos = (
			numpy.cos(trial.stimPositionAngle * numpy.pi/180.0) * trial.eccentricity,
			numpy.sin(trial.stimPositionAngle * numpy.pi/180.0) * trial.eccentricity,
		)

		stimString = 'F:%.2f, O:%.2f, C:%.2f, E:%.2f, P:%.2f' % (frequency, trial.orientation, contrast, trial.eccentricity, trial.stimPositionAngle)
		self.updateHUD('thisStim', stimString)
		expectedLabels = [
			self.config['first_stimulus_key_label'],
			self.config['second_stimulus_key_label'],
		]

		self.updateHUD('expectedResp', expectedLabels[whichStim])

		logging.info(f'Correct stimulus = {whichStim+1}')
		if self.config['wait_for_fixation']:
			self.waitForFixation()

		for i in range(2):
			self.config['sitmulusTone'].play() # play the tone
			if whichStim == i:
				self.stim.contrast = contrast
				if self.config['render_at_gaze']:
					gazePos = self.getGazePosition()
					print('Gaze pos:', gazePos)
					self.stim.pos = [
						self.stim.pos[0] + gazePos[0],
						self.stim.pos[1] + gazePos[1]
					]
			else:
				self.stim.contrast = 0

			self.stim.draw()
			self.win.flip()          # show the stimulus

			time.sleep(self.config['stimulus_duration'] / 1000.0)
			self.win.flip()          # hide the stimulus
			if i < 1:
				time.sleep(self.config['time_between_stimuli'] / 1000.0)     # pause between stimuli

		self.fixationStim.draw()
		self.win.flip()

		correct = self.checkResponse(whichStim)
		self.updateHUD('lastStim', stimString)
		self.updateHUD('thisStim', '')

		if correct:
			logging.debug('Correct response')
			self.updateHUD('lastOk', '✔', (-1, 1, -1))
			self.config['positiveFeedback'].play()
		else:
			logging.debug('Incorrect response')
			self.updateHUD('lastOk', '✘', (1, -1, -1))
			self.config['negativeFeedback'].play()

		self.win.flip()
		logLine = f'E={trial.eccentricity},O={trial.orientation},C={contrast},F={frequency},Correct={correct}'
		logging.info(f'Response: {logLine}')
		stepHandler.markResponse(correct)

	def waitForFixation(self, target=[0,0], threshold=3.5):
		logging.info(f'Waiting for fixation...')
		distance = threshold * 2
		while distance > threshold:
			pos = self.getGazePosition()
			distance = math.sqrt((target[0]-pos[0])**2 + (target[1]-pos[1])**2)

	def getGazePosition(self):
		pos = None
		while pos is None: 
			time.sleep(0.1)
			pos = self.gazeTracker.getPosition()

		return PyPupilGazeTracker.PsychoPyVisuals.screenToMonitorCenterDeg(self.mon, pos)

	def start(self):
		try:
			self.runBlocks()
		except UserExit as exc:
			logging.info(exc)
		except Exception as exc:
			print(exc)
			traceback.print_exc()
			logging.critical(exc)
			self.showMessage('Something went wrong!\n\nPlease let the research assistant know.')

		if self.gazeTracker is not None:
			self.gazeTracker.stop()

		self.fixationStim.autoDraw = False
		self.showMessage('Good job - you are finished with this part of the study!\n\nPress the [SPACEBAR] to exit.')

		self.win.close()
		core.quit()

os.makedirs('data', exist_ok=True)
config = getConfig()
tester = PeripheralCSFTester(config)
tester.start()
