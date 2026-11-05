#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <AVFoundation/AVFoundation.h>
#import <Speech/Speech.h>
#import <stdlib.h>
#import <string.h>
#import <math.h>

// All state lives on the main queue. Audio is never written to disk or logged.
@interface GamjaVoice : NSObject
@property(nonatomic, copy) NSString *token;
@property(nonatomic, copy) NSString *phase;
@property(nonatomic, copy) NSString *text;
@property(nonatomic, strong) SFSpeechRecognizer *recognizer;
@property(nonatomic, strong) AVAudioEngine *engine;
@property(nonatomic, strong) SFSpeechAudioBufferRecognitionRequest *request;
@property(nonatomic, strong) SFSpeechRecognitionTask *task;
@property(nonatomic) BOOL tapInstalled;
@property(nonatomic) BOOL callMode;
@property(nonatomic) BOOL resumeMode;
@property(nonatomic, copy) NSString *language;
- (void)stop:(NSString *)token;
@end
@implementation GamjaVoice
- (instancetype)init { if ((self = [super init])) { _phase = @"idle"; _text = @""; __weak GamjaVoice *weakSelf = self; [[NSNotificationCenter defaultCenter] addObserverForName:NSApplicationDidResignActiveNotification object:nil queue:[NSOperationQueue mainQueue] usingBlock:^(__unused NSNotification *note) { [weakSelf cancel]; }]; } return self; }
- (BOOL)owns:(NSString *)token { return token && [self.token isEqualToString:token]; }
- (void)releaseAudio {
    if (self.tapInstalled) { [self.engine.inputNode removeTapOnBus:0]; self.tapInstalled = NO; }
    [self.engine stop];
    [self.request endAudio];
    self.engine = nil; self.request = nil;
}
- (void)cancel {
    [self releaseAudio]; [self.task cancel]; self.task = nil; self.recognizer = nil;
    self.token = nil; self.text = @""; self.phase = @"idle";
}
- (void)fail:(NSString *)phase token:(NSString *)token {
    if (![self owns:token]) return;
    [self releaseAudio]; [self.task cancel]; self.task = nil; self.recognizer = nil; self.phase = phase;
}
- (BOOL)callAuthorized {
    return [SFSpeechRecognizer authorizationStatus] == SFSpeechRecognizerAuthorizationStatusAuthorized
        && [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio] == AVAuthorizationStatusAuthorized;
}
- (void)beginCapture:(NSString *)token {
    if (![self owns:token] || ![self.phase isEqualToString:@"waiting"]) return;
    if (self.resumeMode && ![self callAuthorized]) { [self fail:@"permission_needed" token:token]; return; }
    SFSpeechRecognizer *recognizer = [[SFSpeechRecognizer alloc] initWithLocale:self.language.length ? [[NSLocale alloc] initWithLocaleIdentifier:self.language] : [NSLocale currentLocale]];
    if (!recognizer || !recognizer.available || !recognizer.supportsOnDeviceRecognition) { [self fail:@"unsupported" token:token]; return; }
    self.recognizer = recognizer;
    self.engine = [[AVAudioEngine alloc] init];
    self.request = [[SFSpeechAudioBufferRecognitionRequest alloc] init];
    self.request.requiresOnDeviceRecognition = YES;
    self.request.shouldReportPartialResults = YES;
    AVAudioInputNode *input = self.engine.inputNode;
    AVAudioFormat *format = [input outputFormatForBus:0];
    if (!input || format.sampleRate <= 0 || format.channelCount == 0 || (self.callMode && format.commonFormat != AVAudioPCMFormatFloat32)) { [self fail:@"error" token:token]; return; }
    SFSpeechAudioBufferRecognitionRequest *request = self.request;
    BOOL callMode = self.callMode;
    __weak GamjaVoice *captureSelf = self;
    __block double voiced = 0, silence = 0;
    __block BOOL stopSent = NO;
    [input installTapOnBus:0 bufferSize:1024 format:format block:^(AVAudioPCMBuffer *buffer, AVAudioTime *when) {
        (void)when; [request appendAudioPCMBuffer:buffer];
        if (!callMode || stopSent || !buffer.floatChannelData || buffer.frameLength == 0) return;
        const float *samples = buffer.floatChannelData[0]; double sum = 0;
        for (AVAudioFrameCount i = 0; i < buffer.frameLength; i++) sum += samples[i] * samples[i];
        double seconds = buffer.frameLength / format.sampleRate;
        if (sqrt(sum / buffer.frameLength) > 0.008) { voiced += seconds; silence = 0; }
        else if (voiced >= 0.25) silence += seconds;
        if (silence >= 1.2) { stopSent = YES; dispatch_async(dispatch_get_main_queue(), ^{ GamjaVoice *v = captureSelf; if ([v owns:token]) [v stop:token]; }); }
    }];
    self.tapInstalled = YES;
    __weak GamjaVoice *weakSelf = self;
    self.task = [recognizer recognitionTaskWithRequest:self.request resultHandler:^(SFSpeechRecognitionResult *result, NSError *error) {
        dispatch_async(dispatch_get_main_queue(), ^{
            GamjaVoice *strongSelf = weakSelf;
            if (!strongSelf || ![strongSelf owns:token] || !([strongSelf.phase isEqualToString:@"listening"] || [strongSelf.phase isEqualToString:@"stopping"])) return;
            if (result) strongSelf.text = result.bestTranscription.formattedString ?: @"";
            if (result.isFinal) { [strongSelf releaseAudio]; strongSelf.task = nil; strongSelf.recognizer = nil; strongSelf.phase = @"review"; }
            else if (error) { [strongSelf fail:@"error" token:token]; }
        });
    }];
    NSError *error = nil;
    if (self.resumeMode && ![self callAuthorized]) { [self fail:@"permission_needed" token:token]; return; }
    [self.engine prepare];
    if (![self.engine startAndReturnError:&error]) { [self fail:@"error" token:token]; return; }
    self.phase = @"listening";
    // A bounded session cannot silently continue if the UI disappears.
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 60 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        GamjaVoice *strongSelf = weakSelf;
        if ([strongSelf owns:token] && [strongSelf.phase isEqualToString:@"listening"]) [strongSelf stop:token];
    });
}
- (void)microphone:(NSString *)token {
    if (![self owns:token]) return;
    AVAuthorizationStatus status = [AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio];
    if (status == AVAuthorizationStatusAuthorized) { [self beginCapture:token]; return; }
    if (status != AVAuthorizationStatusNotDetermined) { [self fail:@"permission_needed" token:token]; return; }
    [AVCaptureDevice requestAccessForMediaType:AVMediaTypeAudio completionHandler:^(BOOL granted) {
        dispatch_async(dispatch_get_main_queue(), ^{ if (![self owns:token]) return; if (granted) [self beginCapture:token]; else [self fail:@"permission_needed" token:token]; });
    }];
}
- (void)start:(NSString *)token call:(BOOL)call language:(NSString *)language {
    if (self.token) return;
    self.callMode = call; self.resumeMode = NO; self.language = language; self.token = token; self.text = @""; self.phase = @"waiting";
    [SFSpeechRecognizer requestAuthorization:^(SFSpeechRecognizerAuthorizationStatus status) {
        dispatch_async(dispatch_get_main_queue(), ^{ if (![self owns:token]) return; if (status == SFSpeechRecognizerAuthorizationStatusAuthorized) [self microphone:token]; else [self fail:@"permission_needed" token:token]; });
    }];
}
// Automatic reconnection never calls either authorization request method. The OS
// owns TCC and may change it independently between checks and capture startup.
- (void)checkCall:(NSString *)token {
    if (self.token) return;
    self.token = token; self.text = @""; self.phase = [self callAuthorized] ? @"authorized" : @"permission_needed";
}
- (void)resumeCall:(NSString *)token language:(NSString *)language {
    if (self.token) return;
    self.callMode = YES; self.resumeMode = YES; self.language = language; self.token = token; self.text = @"";
    if (![self callAuthorized]) { self.phase = @"permission_needed"; return; }
    self.phase = @"waiting"; [self beginCapture:token];
}
- (void)stop:(NSString *)token {
    if (![self owns:token]) return;
    if ([self.phase isEqualToString:@"waiting"]) { [self cancel]; return; }
    if (![self.phase isEqualToString:@"listening"]) return;
    [self releaseAudio]; self.phase = @"stopping";
    // Do not present a partial result as a confirmed final result after a timeout.
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, 10 * NSEC_PER_SEC), dispatch_get_main_queue(), ^{
        if ([self owns:token] && [self.phase isEqualToString:@"stopping"]) [self fail:@"unknown" token:token];
    });
}
@end

static GamjaVoice *service(void) { static GamjaVoice *value; static dispatch_once_t once; dispatch_once(&once, ^{ value = [GamjaVoice new]; }); return value; }
static void onMain(dispatch_block_t block) { if ([NSThread isMainThread]) block(); else dispatch_sync(dispatch_get_main_queue(), block); }
void gamja_voice_action(const char *action, const char *rawToken, const char *rawLanguage) {
    if (!action || !rawToken) return;
    NSString *command = [NSString stringWithUTF8String:action], *token = [NSString stringWithUTF8String:rawToken];
    NSString *language = rawLanguage ? [NSString stringWithUTF8String:rawLanguage] : nil;
    onMain(^{ GamjaVoice *v = service(); if ([command isEqualToString:@"start"] || [command isEqualToString:@"start_call"]) [v start:token call:[command isEqualToString:@"start_call"] language:language]; else if ([command isEqualToString:@"check_call"]) [v checkCall:token]; else if ([command isEqualToString:@"resume_call"]) [v resumeCall:token language:language]; else if ([v owns:token] && [command isEqualToString:@"stop"]) [v stop:token]; else if ([v owns:token] && [command isEqualToString:@"cancel"]) [v cancel]; });
}
char *gamja_voice_snapshot(const char *rawToken) {
    if (!rawToken) return NULL;
    NSString *token = [NSString stringWithUTF8String:rawToken];
    __block char *copy = NULL;
    onMain(^{ GamjaVoice *v = service(); NSDictionary *payload = [v owns:token] ? @{ @"phase":v.phase, @"text":v.text } : @{ @"phase":@"unknown", @"text":@"" }; NSData *data = [NSJSONSerialization dataWithJSONObject:payload options:0 error:nil]; if (data) { NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]; copy = strdup(json.UTF8String); } });
    return copy;
}
