#import <Foundation/Foundation.h>
#import <AppKit/AppKit.h>
#import <AVFoundation/AVFoundation.h>
#import <stdlib.h>
#import <string.h>

@interface GamjaCallSpeech : NSObject <AVSpeechSynthesizerDelegate>
@property(nonatomic, strong) AVSpeechSynthesizer *synth;
@property(nonatomic, strong) AVSpeechUtterance *utterance;
@property(nonatomic, copy) NSString *token;
@property(nonatomic, copy) NSString *phase;
@end
@implementation GamjaCallSpeech
- (instancetype)init { if ((self = [super init])) { _phase = @"idle"; _synth = [AVSpeechSynthesizer new]; _synth.delegate = self; __weak GamjaCallSpeech *weak = self; [[NSNotificationCenter defaultCenter] addObserverForName:NSApplicationDidResignActiveNotification object:nil queue:[NSOperationQueue mainQueue] usingBlock:^(__unused NSNotification *n) { GamjaCallSpeech *s = weak; if (s.token) { [s.synth stopSpeakingAtBoundary:AVSpeechBoundaryImmediate]; s.phase = @"interrupted"; s.utterance = nil; } }]; } return self; }
- (void)speechSynthesizer:(AVSpeechSynthesizer *)synth didFinishSpeechUtterance:(AVSpeechUtterance *)utterance { (void)synth; dispatch_async(dispatch_get_main_queue(), ^{ if (self.utterance == utterance) { self.phase = @"finished"; self.utterance = nil; } }); }
- (void)speechSynthesizer:(AVSpeechSynthesizer *)synth didCancelSpeechUtterance:(AVSpeechUtterance *)utterance { (void)synth; dispatch_async(dispatch_get_main_queue(), ^{ if (self.utterance == utterance) { self.phase = @"interrupted"; self.utterance = nil; } }); }
- (void)start:(NSDictionary *)data {
    NSString *token = data[@"token"], *text = data[@"text"], *voice = data[@"voice"];
    if (self.token || ![token isKindOfClass:NSString.class] || ![text isKindOfClass:NSString.class] || !text.length || text.length > 65536) return;
    AVSpeechSynthesisVoice *selected = nil;
    if ([voice isKindOfClass:NSString.class] && voice.length) { selected = [AVSpeechSynthesisVoice voiceWithIdentifier:voice]; if (!selected) { self.token = token; self.phase = @"unsupported"; return; } }
    AVSpeechUtterance *u = [[AVSpeechUtterance alloc] initWithString:text];
    u.voice = selected;
    NSNumber *rate = data[@"rate"];
    if ([rate isKindOfClass:NSNumber.class]) u.rate = MAX(AVSpeechUtteranceMinimumSpeechRate, MIN(AVSpeechUtteranceMaximumSpeechRate, rate.floatValue));
    self.token = token; self.phase = @"speaking"; self.utterance = u;
    [self.synth speakUtterance:u];
}
- (void)cancel:(NSString *)token { if (![self.token isEqualToString:token]) return; [self.synth stopSpeakingAtBoundary:AVSpeechBoundaryImmediate]; self.utterance = nil; self.token = nil; self.phase = @"idle"; }
@end
static GamjaCallSpeech *service(void) { static GamjaCallSpeech *v; static dispatch_once_t once; dispatch_once(&once, ^{ v=[GamjaCallSpeech new]; }); return v; }
static void onMain(dispatch_block_t block) { if ([NSThread isMainThread]) block(); else dispatch_sync(dispatch_get_main_queue(), block); }
char *gamja_call_speech(const char *raw) {
    if (!raw) return NULL; NSData *input = [[NSString stringWithUTF8String:raw] dataUsingEncoding:NSUTF8StringEncoding]; NSDictionary *args = [NSJSONSerialization JSONObjectWithData:input options:0 error:nil]; if (![args isKindOfClass:NSDictionary.class]) return NULL;
    __block char *copy = NULL;
    onMain(^{ GamjaCallSpeech *s = service(); NSString *action = args[@"action"], *token = args[@"token"]; id result = @{};
        if ([action isEqualToString:@"voices"]) { NSMutableArray *voices = [NSMutableArray array]; for (AVSpeechSynthesisVoice *v in AVSpeechSynthesisVoice.speechVoices) [voices addObject:@{ @"id":v.identifier ?: @"", @"name":v.name ?: @"", @"language":v.language ?: @"" }]; result = voices; }
        else if ([action isEqualToString:@"start"]) { [s start:args]; result = @{ @"phase": [s.token isEqualToString:token] ? s.phase : @"unknown" }; }
        else if ([action isEqualToString:@"cancel"]) { [s cancel:token]; result = @{ @"phase": @"idle" }; }
        else if ([action isEqualToString:@"snapshot"]) result = @{ @"phase": [s.token isEqualToString:token] ? s.phase : @"unknown" };
        NSData *data = [NSJSONSerialization dataWithJSONObject:result options:0 error:nil]; if (data) { NSString *json = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]; copy = strdup(json.UTF8String); }
    }); return copy;
}
