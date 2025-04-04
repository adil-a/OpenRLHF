#!/bin/bash
if [ "$#" -eq 0 ]; then
    echo "Error: Please provide the training command"
    exit 1
fi

# Store the full command
COMMAND="$*"

# Extract parameters using awk
PARAMS=$(echo "$COMMAND" | awk '
{
    # Initialize empty variables
    micro_train = train = micro_rollout = rollout = ""
    max_epochs = gen_max_len = actor_lr = critic_lr = ""
    target_beta = lr_sched = actor_loss = ""
    custom_prompt = actor_mod = adam_beta2 ""
    
    # Scan through all matches in the string
    for(i=1; i<=NF; i++) {
        if($i == "--micro_train_batch_size") micro_train = $(i+1)
        if($i == "--train_batch_size") train = $(i+1)
        if($i == "--micro_rollout_batch_size") micro_rollout = $(i+1)
        if($i == "--rollout_batch_size") rollout = $(i+1)
        if($i == "--max_epochs") max_epochs = $(i+1)
        if($i == "--generate_max_len") gen_max_len = $(i+1)
        if($i == "--actor_learning_rate") actor_lr = $(i+1)
        if($i == "--critic_learning_rate") critic_lr = $(i+1)
        if($i == "--target_dist_beta") target_beta = $(i+1)
        if($i == "--lr_scheduler") lr_sched = $(i+1)
        if($i == "--actor_loss_type") actor_loss = $(i+1)
        if($i == "--custom_single_prompt") custom_prompt = "_custom"
        if($i == "--actor_modulates_base") actor_mod = "_actmod"
        if($i == "--adam_betas") adam_beta2 = "_adambeta2_"$(i+2)
    }
    
    # Print with a special delimiter (|) that wont appear in the values
    if(micro_train != "" && train != "" && micro_rollout != "" && rollout != "" && 
       max_epochs != "" && gen_max_len != "" && actor_lr != "" && critic_lr != "" && 
       target_beta != "" && lr_sched != "" && actor_loss != "")
        print micro_train "|" train "|" micro_rollout "|" rollout "|" max_epochs "|" \
              gen_max_len "|" actor_lr "|" critic_lr "|" target_beta "|" lr_sched "|" \
              actor_loss "|" custom_prompt "|" actor_mod "|" adam_beta2
}')

# Read using the special delimiter
IFS='|' read MICRO_TRAIN TRAIN MICRO_ROLLOUT ROLLOUT MAX_EPOCHS GEN_MAX_LEN \
    ACTOR_LR CRITIC_LR TARGET_BETA LR_SCHED ACTOR_LOSS CUSTOM_PROMPT ACTOR_MOD ADAM_BETA2 <<< "$PARAMS"

# Check if required parameters are empty
if [ -z "$MICRO_TRAIN" ] || [ -z "$TRAIN" ] || [ -z "$MICRO_ROLLOUT" ] || [ -z "$ROLLOUT" ] || \
   [ -z "$MAX_EPOCHS" ] || [ -z "$GEN_MAX_LEN" ] || [ -z "$ACTOR_LR" ] || [ -z "$CRITIC_LR" ] || \
   [ -z "$TARGET_BETA" ] || [ -z "$LR_SCHED" ] || [ -z "$ACTOR_LOSS" ]; then
    echo "Error: Missing required parameters"
    exit 1
fi

#echo $ACTOR_LOSS


# Get current date in required format
CURRENT_DATE=$(date +%Y-%m-%d-%H-%M)

# Generate output filename
PATTERN="${CURRENT_DATE}_batch${MICRO_TRAIN}_${TRAIN}_${MICRO_ROLLOUT}_${ROLLOUT}_ep${MAX_EPOCHS}_len${GEN_MAX_LEN}_${ACTOR_LOSS}_alr${ACTOR_LR}_clr${CRITIC_LR}_beta${TARGET_BETA}_${LR_SCHED}${CUSTOM_PROMPT}${ACTOR_MOD}${ADAM_BETA2}"
SBATCH_FILE="sbatch_${PATTERN}"
OUTPUT_FILE="result_${PATTERN}_s1.txt"

# Create the sbatch file
cat > "$SBATCH_FILE" << EOL
#!/bin/bash
#SBATCH -J s1_$(($RANDOM % 100000))
#SBATCH --ntasks=1
#SBATCH --mem=64G
#SBATCH -c 4
#SBATCH --time=4:00:00
#SBATCH --partition=a40
#SBATCH --qos=m3
#SBATCH --export=ALL
#SBATCH --output=$OUTPUT_FILE
#SBATCH --gres=gpu:1
cd ~
ln -s /usr/bin/gcc-10 .local/bin/gcc
ln -s /usr/bin/g++-10 .local/bin/g++
export PATH=\$HOME/.local/bin/:\$PATH
cd ~/OpenRLHF
source newenv/bin/activate
module load cuda-12.3
deepspeed --master_port $(($RANDOM % 1000 + 3000))1 $COMMAND
EOL

# Make the sbatch file executable
chmod +x "$SBATCH_FILE"
echo "Created sbatch file: $SBATCH_FILE"
echo "Output will be written to: $OUTPUT_FILE"
