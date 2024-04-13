This is the open-source base code for BnuuyBot, the discord bot that does a bazillion things!

================ For anyone looking at the code (please read) ================
DISCLAIMER! This is a pet project that was never meant to be viewed by other people, nor was it designed to be maintained by anyone other than myself. Feel free to poke around, but proceed with caution!
Some features may be hard-coded to only run on the original hardware configuration, so you’ll likely need to mess around with it if you actually want to get this thing running. You will also probably need to install some extra dependencies not listed in requirements.txt, as I wrote this code before I understood virtual environments.

Bnuuybot is an extensively modified version of https://just-some-bots.github.io/MusicBot/, so it uses python and the discord.py API, which you can find plenty of documentation of online. At some point I switched the music downloader from youtube-dl to yt-dlp, so keep that in mind.

To run bnuuy, configure all the stuff (options.ini, etc), and run the run.sh file. 
I think you’ll need something called “Poetry” to launch; This part was done by Katie, so I have no idea how it works. 
You’ll also need to provide your own ffmpeg.exe and ffprobe.exe in the bin folder (unless it auto-installs for you)

Most of the important code is located in the musicbot/bot.py file. It’s really long, but if you scroll to the bottom half, you should start to see the basic patterns of creating your own commands. 
All chat responses (an information about response tags) are located in the responses.txt file.

For privacy and security reasons, this version of the bot has a lot of personalized data stripped out, so you’ll need to do a little tinkering to get it to run on its own. Feel free to add your own personal touches to your bnuuy! Notably, you’ll need to provide your own discord bot token in the options.ini file. Other features that have been stripped down for privacy reasons include (but are not limited to): betabnuuy submissions list, custom emote lists, most of the custom chat responses, ratatouille links, all scoreboard data, birthday lists, jeff’s discord id, and the cookies.txt. If I missed anything, please let me know.

Can’t think of a feature to add? I included my old “Bnuuy TODO” file as a pdf. Pick a random bullet point and get to work!


================ Some fun things Bnuuy can do: ================
+ Music Bot (Join tvoice chat and use &play [song]) 
+ Random bnuuy images (&bnuuy or &betabnuuy) 
+ Submit your own images for the betabnuuy list (send image files to the bnuuy via direct messages, and they will be added automatically) See how many bnuuys people have submitted by using &bnuuyboard! 
+ &emojify [text] will decorate your message with emojis! 
+ Says "funny" stuff! Chat in ⁠bnuuy-general and see how Bnuuy responds! 
+ Occasionally takes a shit! Be the first to clean it up to earn "poop points"! (Check your points with &poops) 
+ Set reminders! (see &help remind) (these will work in direct messages with Bnuuy as well) 
+ Daily wordle clone! (&wordle) 
+ Roll dice for tabletop games! (see &help roll) 
+ Horse Plinko simulator! (&plinko) 
+ Generate a Robert Downey Jr meme! (&rdj [text]) 
+ Makes decisions so you don't have to! (see &help choose) 
+ Generate secure passwords! (see &help generatepassword) 
+ Generate unique names! (&generatename) 
+ Balloon minigame! Don't let it touch the ground! (&balloon) 
+ Play minesweeper! (&minesweeper) 
+ Free hugs! (&hug) 
+ Are you goku? Just keep asking: "Am I Goku?". There's a very small chance you are! Use &goku to see your Goku score! 
+ Custom emotes! Use &emotelist to see the list of emotes you can use. Use &addemote to add to the list! (This feature is kinda broken, please don't abuse it too much) 
+ Typo correction! Mistyped your command? Just edit the original message, and Bnuuy will pretend nothing happened (Only works once per command) 
+ Send anonymous messages to others! (see &help whisper) (Note that the recipient must share at least one server with Bnuuy to receive direct messages) 
+ Other stuff probably