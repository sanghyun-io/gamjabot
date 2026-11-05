#import <Foundation/Foundation.h>
#import <AVFoundation/AVFoundation.h>
#import <math.h>
#import <stdint.h>
#import <unistd.h>
#import <errno.h>
#import <string.h>
#import <time.h>

// Private Core linkage only. No path, microphone, playback, or voice permission API.
typedef int (*GamjaMemoCancelled)(void *);

// The standalone Core service owns this source only on its process main
// thread. The bounded pump is called between existing supervisor operations.
static NSRunLoop *gamjaCoreMainLoop;
static NSPort *gamjaCoreMainPort;
int gamja_core_mainloop_begin(void) {
    @autoreleasepool {
        if (![NSThread isMainThread] || gamjaCoreMainPort) return 0;
        @try {
            NSRunLoop *loop=[NSRunLoop currentRunLoop];
            NSPort *port=[NSMachPort port];
            if (!loop || !port) return 0;
            [loop addPort:port forMode:NSDefaultRunLoopMode];
            gamjaCoreMainLoop=loop; gamjaCoreMainPort=port;
            return 1;
        } @catch (__unused NSException *exception) { return 0; }
    }
}
int gamja_core_mainloop_pump(double seconds) {
    @autoreleasepool {
        if (![NSThread isMainThread] || !gamjaCoreMainLoop || !gamjaCoreMainPort || seconds<0 || seconds>0.025) return 0;
        @try {
            [gamjaCoreMainLoop runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:seconds]];
            return 1;
        } @catch (__unused NSException *exception) { return 0; }
    }
}
void gamja_core_mainloop_end(void) {
    @autoreleasepool {
        if (![NSThread isMainThread] || !gamjaCoreMainPort) return;
        @try { [gamjaCoreMainLoop removePort:gamjaCoreMainPort forMode:NSDefaultRunLoopMode]; }
        @catch (__unused NSException *exception) {}
        gamjaCoreMainPort=nil; gamjaCoreMainLoop=nil;
    }
}

int gamja_core_speech_available(void) { if (@available(macOS 14.0, *)) return 1; return 0; }
char *gamja_core_speech_voices(void) {
    @autoreleasepool {
        if (@available(macOS 14.0, *)) {
            NSMutableArray *result=[NSMutableArray array];
            NSArray *voices=[AVSpeechSynthesisVoice.speechVoices sortedArrayUsingComparator:^NSComparisonResult(AVSpeechSynthesisVoice *a, AVSpeechSynthesisVoice *b) { return [a.identifier compare:b.identifier]; }];
            for (AVSpeechSynthesisVoice *voice in voices) {
                NSString *identifier=voice.identifier, *locale=voice.language;
                if (![identifier hasPrefix:@"com.apple."] || (voice.voiceTraits & AVSpeechSynthesisVoiceTraitIsPersonalVoice) || !locale.length ||
                    [identifier lengthOfBytesUsingEncoding:NSUTF8StringEncoding]>200 || [locale lengthOfBytesUsingEncoding:NSUTF8StringEncoding]>64) continue;
                // The SDK documents nil here for voices not available on device.
                if (![AVSpeechSynthesisVoice voiceWithIdentifier:identifier]) continue;
                [result addObject:@{@"id":identifier,@"locale":locale}];
                if (result.count==128) break;
            }
            NSData *data=[NSJSONSerialization dataWithJSONObject:result options:0 error:nil];
            if (!data || data.length>65536) return NULL;
            NSString *json=[[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
            return json ? strdup(json.UTF8String) : NULL;
        }
        return NULL;
    }
}
static BOOL write_all(int fd, const void *p, size_t n) {
    const uint8_t *b = p;
    while (n) { ssize_t k = write(fd, b, n); if (k < 0 && errno == EINTR) continue; if (k <= 0) return NO; b += k; n -= (size_t)k; }
    return YES;
}
static void le16(uint8_t *p, uint16_t n) { p[0] = n; p[1] = n >> 8; }
static void le32(uint8_t *p, uint32_t n) { p[0]=n; p[1]=n>>8; p[2]=n>>16; p[3]=n>>24; }
static double monotonic_seconds(void) { struct timespec t; return clock_gettime(CLOCK_MONOTONIC,&t)==0 ? (double)t.tv_sec+(double)t.tv_nsec/1e9 : -1; }

@interface GamjaCoreMemo : NSObject
@property(nonatomic, strong) NSLock *lock;
@property(nonatomic, strong) AVSpeechSynthesizer *synth;
@property(nonatomic) int fd;
@property(nonatomic) BOOL finished;
@property(nonatomic) int status;
@property(nonatomic) uint64_t sourceFrames;
@property(nonatomic) uint64_t outputFrames;
@property(nonatomic) uint32_t sourceRate;
@end
@implementation GamjaCoreMemo
- (void)finishLocked:(int)status {
    if (self.finished) return;
    self.finished = YES; self.status = status;
}
- (void)buffer:(AVAudioBuffer *)audio {
    [self.lock lock];
    if (self.finished) { [self.lock unlock]; return; }
    if (![audio isKindOfClass:AVAudioPCMBuffer.class]) { [self finishLocked:2]; [self.lock unlock]; return; }
    AVAudioPCMBuffer *pcm = (AVAudioPCMBuffer *)audio;
    if (!pcm.frameLength) { [self finishLocked:self.outputFrames ? 0 : 2]; [self.lock unlock]; return; }
    AVAudioFormat *fmt = pcm.format;
    double hz = fmt.sampleRate;
    uint32_t channels = fmt.channelCount;
    if (!isfinite(hz) || hz < 8000 || hz > 192000 || floor(hz) != hz || !channels || channels > 8 ||
        (fmt.commonFormat != AVAudioPCMFormatFloat32 && fmt.commonFormat != AVAudioPCMFormatInt16) ||
        (self.sourceRate && self.sourceRate != (uint32_t)hz)) {
        [self finishLocked:2]; [self.lock unlock]; return;
    }
    self.sourceRate = (uint32_t)hz;
    const AudioBufferList *list = pcm.audioBufferList;
    BOOL interleaved = fmt.interleaved;
    if (!list || list->mNumberBuffers != (interleaved ? 1 : channels)) { [self finishLocked:2]; [self.lock unlock]; return; }
    size_t unit = fmt.commonFormat == AVAudioPCMFormatFloat32 ? sizeof(float) : sizeof(int16_t);
    for (UInt32 c=0; c<list->mNumberBuffers; ++c) {
        size_t need = (size_t)pcm.frameLength * unit * (interleaved ? channels : 1);
        if (!list->mBuffers[c].mData || list->mBuffers[c].mDataByteSize < need) { [self finishLocked:2]; [self.lock unlock]; return; }
    }
    uint64_t end = self.sourceFrames + pcm.frameLength;
    uint8_t out[4096]; size_t used = 0;
    while (self.outputFrames * (uint64_t)self.sourceRate / 22050 < end) {
        uint64_t source = self.outputFrames * (uint64_t)self.sourceRate / 22050;
        if (source < self.sourceFrames || self.outputFrames >= 22050ULL * 300 || 44 + (self.outputFrames + 1) * 2 > 10 * 1024 * 1024) {
            [self finishLocked:3]; break;
        }
        size_t index = (size_t)(source - self.sourceFrames);
        double sample = 0;
        for (uint32_t c=0; c<channels; ++c) {
            const uint8_t *base = list->mBuffers[interleaved ? 0 : c].mData;
            size_t offset = (interleaved ? index * channels + c : index) * unit;
            if (fmt.commonFormat == AVAudioPCMFormatFloat32) { float v; memcpy(&v,base+offset,sizeof(v)); if (!isfinite(v)) { [self finishLocked:2]; break; } sample += v; }
            else { int16_t v; memcpy(&v,base+offset,sizeof(v)); sample += (double)v / 32768.0; }
        }
        if (self.finished) break;
        sample = fmax(-1.0, fmin(1.0, sample / channels));
        int16_t value = (int16_t)lrint(sample * (sample < 0 ? 32768.0 : 32767.0));
        le16(out+used, (uint16_t)value); used += 2; self.outputFrames++;
        if (used == sizeof(out)) { if (!write_all(self.fd,out,used)) { [self finishLocked:4]; break; } used=0; }
    }
    if (!self.finished && used && !write_all(self.fd,out,used)) [self finishLocked:4];
    self.sourceFrames = end;
    [self.lock unlock];
}
@end

// Returns zero only for a complete WAV. Caller owns fd and removes failures.
int gamja_core_speech_memo(int fd, const char *text, const char *voice, const char *locale,
                           int rate_milli, GamjaMemoCancelled cancelled, void *ctx, uint64_t *frames) {
    @autoreleasepool {
        if (@available(macOS 14.0, *)) {
            if (fd < 0 || !text || !voice || !locale || !cancelled || !frames) return 1;
            NSString *t=[NSString stringWithUTF8String:text], *v=[NSString stringWithUTF8String:voice], *l=[NSString stringWithUTF8String:locale];
            if (!t || !v || !l || !t.length || [t lengthOfBytesUsingEncoding:NSUTF8StringEncoding] > 4000 ||
                ![v hasPrefix:@"com.apple."] || !l.length) return 1;
            float rate = (float)rate_milli / 1000.0f;
            if (rate < AVSpeechUtteranceMinimumSpeechRate || rate > AVSpeechUtteranceMaximumSpeechRate) return 1;
            AVSpeechSynthesisVoice *selected = [AVSpeechSynthesisVoice voiceWithIdentifier:v];
            if (!selected || (selected.voiceTraits & AVSpeechSynthesisVoiceTraitIsPersonalVoice) || ![selected.language isEqualToString:l]) return 1;
            uint8_t header[44] = {0}; memcpy(header,"RIFF",4); memcpy(header+8,"WAVEfmt ",8); le32(header+16,16); le16(header+20,1); le16(header+22,1); le32(header+24,22050); le32(header+28,44100); le16(header+32,2); le16(header+34,16); memcpy(header+36,"data",4);
            if (!write_all(fd,header,sizeof(header))) return 4;
            GamjaCoreMemo *s=[GamjaCoreMemo new]; s.lock=[NSLock new]; s.fd=fd; s.synth=[AVSpeechSynthesizer new];
            AVSpeechUtterance *u=[[AVSpeechUtterance alloc] initWithString:t]; u.voice=selected; u.rate=rate;
            AVSpeechSynthesizer *synth=s.synth;
            // Core is a CLI service and has no GUI/main-queue run loop. Own a
            // run loop on this request's worker and keep all synth operations
            // on that thread while buffers and cancellation are processed.
            NSRunLoop *runLoop=[NSRunLoop currentRunLoop];
            NSPort *keepAlive=[NSMachPort port];
            if (!keepAlive) return 5;
            [runLoop addPort:keepAlive forMode:NSDefaultRunLoopMode];
            double started=monotonic_seconds();
            @try {
                if (started<0 || cancelled(ctx)) { [s.lock lock]; [s finishLocked:5]; [s.lock unlock]; }
                else [synth writeUtterance:u toBufferCallback:^(AVAudioBuffer *b) { [s buffer:b]; }];
                for (;;) {
                    [s.lock lock]; BOOL finished=s.finished; [s.lock unlock];
                    if (finished) break;
                    double now=monotonic_seconds();
                    if (cancelled(ctx) || now<0 || now-started>=300.0) { [s.lock lock]; [s finishLocked:5]; [s.lock unlock]; break; }
                    @autoreleasepool {
                        [runLoop runMode:NSDefaultRunLoopMode beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
                    }
                }
            } @catch (__unused NSException *exception) {
                [s.lock lock]; [s finishLocked:5]; [s.lock unlock];
            }
            [s.lock lock]; int status=s.status; uint64_t n=s.outputFrames; s.synth=nil; [s.lock unlock];
            double ended=monotonic_seconds();
            if (status==0 && (cancelled(ctx) || ended<0 || ended-started>=300.0)) status=5;
            if (status != 0) { @try { [synth stopSpeakingAtBoundary:AVSpeechBoundaryImmediate]; } @catch (__unused NSException *exception) {} }
            [runLoop removePort:keepAlive forMode:NSDefaultRunLoopMode];
            if (status != 0) return status;
            uint32_t bytes=(uint32_t)(n*2); le32(header+4,36+bytes); le32(header+40,bytes);
            if (pwrite(fd,header,sizeof(header),0) != sizeof(header)) return 4;
            *frames=n; return 0;
        }
        return 1;
    }
}
